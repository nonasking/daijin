// 12c — Funnel(공개 TLS) 연결 안정화 버전.
// 개선: keepalive 60s, socket timeout 30s, 고유 client ID(세션충돌 방지), WiFi 자동재연결.
// (진단상 setInsecure 유지 — 안정화 확인 후 CA+NTP 정식버전으로)
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <PubSubClient.h>
#include "secrets.h"   // WIFI_SSID, WIFI_PASS, MQTT_USER, MQTT_PASS

#define RGB_PIN 48
const char* MQTT_HOST = "macbookpro.tail9f8fdd.ts.net";
const int   MQTT_PORT = 8443;
WiFiClientSecure net;
PubSubClient mqtt(net);
const char* TOPIC_SET = "home/led/set";
const char* TOPIC_STATE = "home/led/state";
String clientId;

void setColor(const String& m){
  if (m=="off") neopixelWrite(RGB_PIN,0,0,0);
  else if (m=="red") neopixelWrite(RGB_PIN,40,0,0);
  else if (m=="blue") neopixelWrite(RGB_PIN,0,0,40);
  else if (m=="yellow") neopixelWrite(RGB_PIN,40,40,0);
  else if (m=="magenta") neopixelWrite(RGB_PIN,40,0,40);
  else if (m=="cyan") neopixelWrite(RGB_PIN,0,40,40);
  else neopixelWrite(RGB_PIN,0,40,0);
}
void onMessage(char* t, byte* p, unsigned int n){
  String m; for(unsigned int i=0;i<n;i++) m+=(char)p[i];
  setColor(m);
  mqtt.publish(TOPIC_STATE, m.c_str(), true);
}

void ensureWifi(){
  if (WiFi.status()==WL_CONNECTED) return;
  WiFi.mode(WIFI_STA); WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status()!=WL_CONNECTED){
    neopixelWrite(RGB_PIN,8,8,8); delay(150); neopixelWrite(RGB_PIN,0,0,0); delay(150);
  }
}

void setup(){
  Serial.begin(115200);
  clientId = "esp32-led-" + String((uint32_t)(ESP.getEfuseMac() & 0xFFFFFF), HEX);
  ensureWifi();
  net.setInsecure();
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMessage);
  mqtt.setKeepAlive(60);       // 인터넷 왕복 지연 대비 (기본 15s → 60s)
  mqtt.setSocketTimeout(30);   // 소켓 타임아웃 여유
}

void loop(){
  ensureWifi();
  if (!mqtt.connected()){
    neopixelWrite(RGB_PIN,40,0,0);                 // 빨강=연결 시도 중
    if (mqtt.connect(clientId.c_str(), MQTT_USER, MQTT_PASS)){
      mqtt.subscribe(TOPIC_SET);
      neopixelWrite(RGB_PIN,0,40,0);               // 초록=연결됨
      mqtt.publish(TOPIC_STATE, "green", true);
    } else {
      delay(3000);
    }
  }
  mqtt.loop();
}
