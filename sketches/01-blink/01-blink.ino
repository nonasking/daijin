// 01-blink — 보드 살아있는지 확인 (도착 첫날 1번)
// ESP32-S3는 단순 LED가 아니라 온보드 RGB(WS2812)가 GPIO48에 달려 있음.
// neopixelWrite()는 ESP32 Arduino 코어 기본 제공 함수라 라이브러리 불필요.
//
// 업로드:
//   arduino-cli compile --fqbn esp32:esp32:esp32s3 sketches/01-blink
//   arduino-cli upload  --fqbn esp32:esp32:esp32s3 -p /dev/cu.usbXXXX sketches/01-blink

#define RGB_PIN 48   // Freenove ESP32-S3 / DevKitC-1 온보드 RGB LED

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n[01-blink] boot OK — 보드 정상 동작 중");
}

void loop() {
  neopixelWrite(RGB_PIN, 32, 0, 0);  // 빨강
  Serial.println("LED: RED");
  delay(500);
  neopixelWrite(RGB_PIN, 0, 32, 0);  // 초록
  Serial.println("LED: GREEN");
  delay(500);
  neopixelWrite(RGB_PIN, 0, 0, 32);  // 파랑
  Serial.println("LED: BLUE");
  delay(500);
}
