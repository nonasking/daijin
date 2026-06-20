// 05-pir — PIR 인체감지 (부모님댁 "안부" 핵심 센서: 움직임 = 활동 중)
// FNK0082 PIR 모듈: VCC=5V, GND=GND, OUT=신호핀(GPIO 4 예시, 라이브러리 불필요)
// 사람이 움직이면 OUT이 HIGH. 일정 시간 움직임 없으면 LOW → "장시간 무활동" 알림 로직의 기반.

const int PIR_PIN = 4;
unsigned long lastMotion = 0;

void setup() {
  Serial.begin(115200);
  pinMode(PIR_PIN, INPUT);
  Serial.println("[05-pir] 워밍업 30초… (PIR는 전원 후 안정화 필요)");
}

void loop() {
  if (digitalRead(PIR_PIN) == HIGH) {
    lastMotion = millis();
    Serial.println("👤 움직임 감지");
    delay(1000);
  } else {
    unsigned long idle = (millis() - lastMotion) / 1000;
    Serial.printf("… 무활동 %lus\n", idle);   // 운영 시: 이 값이 임계치 넘으면 MQTT 알림
    delay(2000);
  }
}
