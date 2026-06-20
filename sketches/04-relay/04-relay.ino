// 04-relay — 릴레이 ON/OFF (220V 가전 스위치의 기본)
// FNK0082 릴레이 모듈: VCC=5V, GND=GND, IN=신호핀(GPIO 14 예시)
// "찰칵" 소리 + 모듈 LED로 동작 확인. 처음엔 220V 연결 없이 똑딱임만 확인할 것!
// ⚠️ 220V 부하 연결은 감전/화재 위험 — 부품 감 잡힌 뒤, 충분히 학습 후에만.

const int RELAY_PIN = 14;

void setup() {
  Serial.begin(115200);
  pinMode(RELAY_PIN, OUTPUT);
  Serial.println("[04-relay] 1초 간격 ON/OFF — 찰칵 소리 확인");
}

void loop() {
  digitalWrite(RELAY_PIN, HIGH);  // 모듈에 따라 LOW가 ON일 수 있음(active-low)
  Serial.println("RELAY ON");
  delay(1000);
  digitalWrite(RELAY_PIN, LOW);
  Serial.println("RELAY OFF");
  delay(1000);
}
