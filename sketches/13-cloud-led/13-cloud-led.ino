// 13-cloud-led — 이동형 ESP32: HiveMQ 클라우드 브로커(TLS 8883)로 어디서나 LED 제어.
// 현장 WiFi = WiFiManager 캡티브 포털로 입력(하드코딩 X). NTP 타임아웃 + 논블로킹 재연결.
// 참고: EMQX esp32_connect_mqtt_via_tls.ino + PubSubClient mqtt_reconnect_nonblocking.ino
// 자격증명=secrets.h, CA=ca_cert.h (같은 폴더, gitignore=secrets.h).

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <WiFiManager.h>          // tzapu/WiFiManager
#include <PubSubClient.h>
#include "secrets.h"             // MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASS
#include "ca_cert.h"             // ISRG_ROOT_X1

#define RGB_PIN 48
WiFiClientSecure net;
PubSubClient     mqtt(net);
WiFiManager      wm;
String clientId;
const char* TOPIC_SET   = "home/led/set";
const char* TOPIC_STATE = "home/led/state";

void setColor(const String& m) {
  if      (m=="off")     neopixelWrite(RGB_PIN,0,0,0);
  else if (m=="red")     neopixelWrite(RGB_PIN,40,0,0);
  else if (m=="blue")    neopixelWrite(RGB_PIN,0,0,40);
  else if (m=="yellow")  neopixelWrite(RGB_PIN,40,40,0);
  else if (m=="magenta") neopixelWrite(RGB_PIN,40,0,40);
  else if (m=="cyan")    neopixelWrite(RGB_PIN,0,40,40);
  else if (m=="white")   neopixelWrite(RGB_PIN,40,40,40);
  else                   neopixelWrite(RGB_PIN,0,40,0);  // green/그외
}
void onMessage(char* t, byte* p, unsigned int n) {
  String m; for (unsigned int i=0;i<n;i++) m+=(char)p[i];
  Serial.printf("[MQTT] %s\n", m.c_str());
  setColor(m);
  mqtt.publish(TOPIC_STATE, m.c_str(), true);
}

// 설정 포털 진입 시(저장된 WiFi 없음) → 보라색 = "폰으로 ESP32-Setup 접속해 WiFi 입력하세요"
void onConfigPortal(WiFiManager* w) { neopixelWrite(RGB_PIN, 30, 0, 30); }

bool syncTime(uint32_t timeoutMs) {   // TLS 인증서 검증에 시간 필요. 타임아웃으로 멈춤 방지.
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
  uint32_t start = millis(); time_t now = time(nullptr);
  while (now < 1700000000 && millis() - start < timeoutMs) { delay(200); now = time(nullptr); }
  return now >= 1700000000;
}

void setup() {
  Serial.begin(115200); delay(200);
  clientId = "esp32-" + String((uint32_t)(ESP.getEfuseMac() & 0xFFFFFF), HEX);
  neopixelWrite(RGB_PIN, 8, 8, 8);                 // 부팅

  // --- 현장 WiFi 프로비저닝 ---
  wm.setAPCallback(onConfigPortal);
  wm.setConfigPortalTimeout(300);                  // 5분 내 미설정이면 재시도
  if (!wm.autoConnect("ESP32-Setup")) { ESP.restart(); }  // 저장WiFi 실패→AP포털
  Serial.printf("WiFi OK: %s\n", WiFi.localIP().toString().c_str());

  // --- TLS (시간 동기화 후 CA 검증; NTP 실패 시 암호화만) ---
  if (syncTime(12000)) { net.setCACert(ISRG_ROOT_X1); Serial.println("time OK → CA 검증"); }
  else { net.setInsecure(); Serial.println("NTP 실패 → setInsecure(암호화만)"); }

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMessage);
  mqtt.setKeepAlive(60);
  mqtt.setSocketTimeout(30);
}

void loop() {
  if (!mqtt.connected()) {
    neopixelWrite(RGB_PIN, 40, 0, 0);              // 빨강 = 브로커 연결 시도
    Serial.print("MQTT 연결...");
    if (mqtt.connect(clientId.c_str(), MQTT_USER, MQTT_PASS)) {
      Serial.println(" OK");
      mqtt.subscribe(TOPIC_SET);
      neopixelWrite(RGB_PIN, 0, 40, 0);            // 초록 = 연결됨
      mqtt.publish(TOPIC_STATE, "green", true);
    } else {
      Serial.printf(" 실패(rc=%d)\n", mqtt.state());
      delay(3000);
    }
  }
  mqtt.loop();
}
