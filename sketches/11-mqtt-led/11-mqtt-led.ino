// 11-mqtt-led — MQTT로 LED 원격 제어 (HTTP 버전을 MQTT 인프라로 교체)
// 흐름: 누군가 토픽 home/led/set 에 색을 publish → 보드가 구독해서 LED 변경
// 제어(맥/폰/Claude): mosquitto_pub -h <broker> -u iot -P <pw> -t home/led/set -m green
// 자격증명은 secrets.h(같은 폴더, gitignore됨)에서 가져옴.

#include <WiFi.h>
#include <PubSubClient.h>
#include "secrets.h"   // WIFI_SSID, WIFI_PASS, MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASS

#define RGB_PIN 48
WiFiClient   net;
PubSubClient mqtt(net);

const char* TOPIC_SET   = "home/led/set";    // 명령 받는 토픽
const char* TOPIC_STATE = "home/led/state";  // 현재 상태 알리는 토픽 (retained)

struct Color { const char* name; uint8_t r, g, b; };
Color COLORS[] = {
  {"red",40,0,0},{"green",0,40,0},{"blue",0,0,40},{"yellow",40,40,0},
  {"cyan",0,40,40},{"magenta",40,0,40},{"white",40,40,40},{"orange",40,14,0},{"off",0,0,0},
};
const int NCOLORS = sizeof(COLORS)/sizeof(COLORS[0]);
String lastColor = "off";

void setLed(String name) {
  name.trim();
  for (int i = 0; i < NCOLORS; i++) {
    if (name == COLORS[i].name) {
      neopixelWrite(RGB_PIN, COLORS[i].r, COLORS[i].g, COLORS[i].b);
      lastColor = name;
      mqtt.publish(TOPIC_STATE, name.c_str(), true);   // retained 상태 보고
      Serial.printf("LED -> %s\n", name.c_str());
      return;
    }
  }
  Serial.printf("unknown color: %s\n", name.c_str());
}

void onMessage(char* topic, byte* payload, unsigned int len) {
  String msg;
  for (unsigned int i = 0; i < len; i++) msg += (char)payload[i];
  Serial.printf("[MQTT] %s = %s\n", topic, msg.c_str());
  setLed(msg);
}

void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("WiFi 연결 중");
  while (WiFi.status() != WL_CONNECTED) {
    neopixelWrite(RGB_PIN, 8, 8, 8); delay(150);   // 하양 깜빡 = 대기
    neopixelWrite(RGB_PIN, 0, 0, 0); delay(150);
    Serial.print(".");
  }
  Serial.printf("\nWiFi OK: %s\n", WiFi.localIP().toString().c_str());
}

void connectMqtt() {
  while (!mqtt.connected()) {
    Serial.print("MQTT 연결 중...");
    if (mqtt.connect("esp32-led", MQTT_USER, MQTT_PASS)) {
      Serial.println(" OK");
      mqtt.subscribe(TOPIC_SET);
      neopixelWrite(RGB_PIN, 0, 40, 0);   // 초록 = 연결됨
      lastColor = "green";
      mqtt.publish(TOPIC_STATE, "green", true);
    } else {
      Serial.printf(" 실패(rc=%d) 2초 후 재시도\n", mqtt.state());
      neopixelWrite(RGB_PIN, 40, 0, 0);   // 빨강 = MQTT 실패
      delay(2000);
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(200);
  connectWifi();
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMessage);
  connectMqtt();
}

void loop() {
  if (!mqtt.connected()) connectMqtt();
  mqtt.loop();
}
