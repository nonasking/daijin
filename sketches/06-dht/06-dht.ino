// 06-dht — 온습도 측정 (부모님댁 환경 모니터링: 폭염/한파/건조 알림 기반)
// FNK0082 DHT11(또는 DHT22): VCC=3.3~5V, GND=GND, DATA=신호핀(GPIO 5 예시)
// 라이브러리: arduino-cli lib install "DHT sensor library"  (Adafruit Unified Sensor 의존성 자동 설치)

#include "DHT.h"

#define DHT_PIN  5
#define DHT_TYPE DHT11   // DHT22면 DHT22로 변경

DHT dht(DHT_PIN, DHT_TYPE);

void setup() {
  Serial.begin(115200);
  dht.begin();
  Serial.println("[06-dht] 온습도 측정 시작 (2초 간격)");
}

void loop() {
  float h = dht.readHumidity();
  float t = dht.readTemperature();
  if (isnan(h) || isnan(t)) {
    Serial.println("⚠️ 센서 읽기 실패 — 배선 확인");
  } else {
    Serial.printf("🌡️ %.1f°C   💧 %.0f%%\n", t, h);  // 운영 시: MQTT publish
  }
  delay(2000);
}
