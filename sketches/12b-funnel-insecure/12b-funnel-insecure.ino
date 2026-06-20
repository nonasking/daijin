// 12b — 진단용: Funnel TLS를 인증서검증/NTP 없이(setInsecure) 시도.
// 목적: 붙으면 → 원인은 CA cert 또는 시간(NTP). 안 붙으면 → 네트워크/메모리/auth.
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

void onMessage(char* t, byte* p, unsigned int n) {
  String m; for (unsigned int i=0;i<n;i++) m+=(char)p[i];
  if (m=="off") neopixelWrite(RGB_PIN,0,0,0);
  else if (m=="red") neopixelWrite(RGB_PIN,40,0,0);
  else if (m=="blue") neopixelWrite(RGB_PIN,0,0,40);
  else neopixelWrite(RGB_PIN,0,40,0);
  mqtt.publish(TOPIC_STATE, m.c_str(), true);
}

void setup() {
  Serial.begin(115200);
  WiFi.mode(WIFI_STA); WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status()!=WL_CONNECTED){ neopixelWrite(RGB_PIN,8,8,8); delay(150); neopixelWrite(RGB_PIN,0,0,0); delay(150); }
  net.setInsecure();                     // ← 인증서 검증 건너뜀 (시간 불필요)
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMessage);
}

void loop() {
  if (!mqtt.connected()) {
    if (mqtt.connect("esp32-led-insec", MQTT_USER, MQTT_PASS)) {
      mqtt.subscribe(TOPIC_SET);
      neopixelWrite(RGB_PIN,0,40,0);                 // 초록=접속
      mqtt.publish(TOPIC_STATE,"green",true);
    } else {
      neopixelWrite(RGB_PIN,40,0,0); delay(3000);    // 빨강=실패
    }
  }
  mqtt.loop();
}
