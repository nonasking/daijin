// 03-servo — 서보 0→180→0 sweep (물리 제어 첫 감)
// FNK0082 서보(SG90 계열) 배선: 갈색=GND, 빨강=5V, 주황=신호핀(GPIO 13 예시)
// 라이브러리: ESP32 코어 3.x의 ESP32Servo (보통 내장). 없으면:
//   arduino-cli lib install "ESP32Servo"

#include <ESP32Servo.h>

const int SERVO_PIN = 13;   // 신호선 연결 핀 (원하는 GPIO로)
Servo servo;

void setup() {
  Serial.begin(115200);
  servo.attach(SERVO_PIN);
  Serial.println("[03-servo] sweep 시작");
}

void loop() {
  for (int a = 0; a <= 180; a += 5) { servo.write(a); delay(20); }
  Serial.println("→ 180도");
  for (int a = 180; a >= 0; a -= 5) { servo.write(a); delay(20); }
  Serial.println("→ 0도");
}
