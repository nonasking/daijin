// 12-mqtt-led-funnel — ESP32가 Funnel(공개 TLS)로 브로커에 붙어 LED 제어
// → ESP32가 브로커(맥)와 "다른 네트워크"에 있어도 인터넷 경유로 동작.
// 차이점(11번 대비): WiFiClientSecure + TLS, 브로커 주소 = Funnel 공개주소:8443, NTP 시간동기화.
// 자격증명=secrets.h, CA=ca_cert.h (둘 다 같은 폴더).

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <PubSubClient.h>
#include "secrets.h"   // WIFI_SSID, WIFI_PASS, MQTT_USER, MQTT_PASS
#include "ca_cert.h"   // ISRG_ROOT_X1

#define RGB_PIN 48
const char* MQTT_HOST = "macbookpro.tail9f8fdd.ts.net";  // Funnel 공개주소 (비밀 아님)
const int   MQTT_PORT = 8443;                            // Funnel TLS 포트

WiFiClientSecure net;
PubSubClient     mqtt(net);
const char* TOPIC_SET   = "home/led/set";
const char* TOPIC_STATE = "home/led/state";

struct Color { const char* name; uint8_t r, g, b; };
Color COLORS[] = {
  {"red",40,0,0},{"green",0,40,0},{"blue",0,0,40},{"yellow",40,40,0},
  {"cyan",0,40,40},{"magenta",40,0,40},{"white",40,40,40},{"orange",40,14,0},{"off",0,0,0},
};
const int NCOLORS = sizeof(COLORS)/sizeof(COLORS[0]);

void setLed(String name) {
  name.trim();
  for (int i = 0; i < NCOLORS; i++) {
    if (name == COLORS[i].name) {
      neopixelWrite(RGB_PIN, COLORS[i].r, COLORS[i].g, COLORS[i].b);
      mqtt.publish(TOPIC_STATE, name.c_str(), true);
      Serial.printf("LED -> %s\n", name.c_str());
      return;
    }
  }
  Serial.printf("unknown color: %s\n", name.c_str());
}

void onMessage(char* topic, byte* payload, unsigned int len) {
  String msg; for (unsigned int i = 0; i < len; i++) msg += (char)payload[i];
  Serial.printf("[MQTT] %s = %s\n", topic, msg.c_str());
  setLed(msg);
}

void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("WiFi 연결 중");
  while (WiFi.status() != WL_CONNECTED) {
    neopixelWrite(RGB_PIN, 8, 8, 8); delay(150);
    neopixelWrite(RGB_PIN, 0, 0, 0); delay(150);
    Serial.print(".");
  }
  Serial.printf("\nWiFi OK: %s\n", WiFi.localIP().toString().c_str());
}

void syncTime() {  // TLS 인증서 검증에 정확한 시간 필요
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
  Serial.print("시간 동기화");
  time_t now = time(nullptr);
  while (now < 1700000000) { delay(200); Serial.print("."); now = time(nullptr); }
  Serial.printf("\n시간 OK: %ld\n", now);
}

void connectMqtt() {
  while (!mqtt.connected()) {
    Serial.print("MQTT(TLS) 연결 중...");
    if (mqtt.connect("esp32-led-funnel", MQTT_USER, MQTT_PASS)) {
      Serial.println(" OK");
      mqtt.subscribe(TOPIC_SET);
      neopixelWrite(RGB_PIN, 0, 40, 0);           // 초록 = 연결됨
      mqtt.publish(TOPIC_STATE, "green", true);
    } else {
      Serial.printf(" 실패(rc=%d) 3초 후 재시도\n", mqtt.state());
      neopixelWrite(RGB_PIN, 40, 0, 0);           // 빨강 = 실패
      delay(3000);
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(200);
  connectWifi();
  syncTime();
  net.setCACert(ISRG_ROOT_X1);                    // Funnel 서버 인증서 검증
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMessage);
  connectMqtt();
}

void loop() {
  if (!mqtt.connected()) connectMqtt();
  mqtt.loop();
}
