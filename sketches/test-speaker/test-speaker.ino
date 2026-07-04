// test-speaker — Audio Converter & Amplifier 모듈 테스트 (Freenove Ch29.2 배선)
// 부팅 시 + BOOT 버튼 누를 때마다 도-미-솔 멜로디 재생
// 배선: BCK=GPIO14, LCK=GPIO12, DIN=GPIO13, VCC=5V, GND=GND, SCK=미연결
// 스피커는 모듈 L+/L- 에 연결
#include <ESP_I2S.h>
#include <math.h>

#define RGB_PIN 48
#define BTN     0
#define SPK_BCK 14
#define SPK_LRC 12
#define SPK_DIN 13

I2SClass I2S;
inline void led(uint8_t r,uint8_t g,uint8_t b){ neopixelWrite(RGB_PIN,r,g,b); }

const int SR = 22050;
static int16_t buf[2048];   // 스테레오 프레임 1024개

// freq(Hz) 톤을 ms 밀리초 재생
void tone_ms(float freq, int ms) {
  static float phase = 0;
  int frames_left = SR * ms / 1000;
  while (frames_left > 0) {
    int n = min(frames_left, 1024);
    for (int i = 0; i < n; i++) {
      int16_t s = (int16_t)(8000 * sinf(phase));
      phase += 2 * PI * freq / SR;
      if (phase > 2 * PI) phase -= 2 * PI;
      buf[i*2] = s; buf[i*2+1] = s;         // L, R
    }
    I2S.write((uint8_t*)buf, n * 4);
    frames_left -= n;
  }
}

void melody() {
  led(0,0,40);
  tone_ms(523.25, 300);   // 도(C5)
  tone_ms(659.25, 300);   // 미(E5)
  tone_ms(783.99, 450);   // 솔(G5)
  tone_ms(0.01, 60);      // 무음 마무리 (팝 방지)
  led(0,0,0);
}

void setup() {
  Serial.begin(115200); Serial0.begin(115200);
  pinMode(BTN, INPUT_PULLUP);
  I2S.setPins(SPK_BCK, SPK_LRC, SPK_DIN, -1, -1);  // bclk, ws, dout, din(-1), mclk(-1)
  if (!I2S.begin(I2S_MODE_STD, SR, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO)) {
    Serial.println("I2S begin fail"); Serial0.println("I2S begin fail");
    led(40,0,0); while(1) delay(1000);
  }
  led(0,30,0); delay(300); led(0,0,0);
  Serial.println("ready - BOOT to play"); Serial0.println("ready - BOOT to play");
  melody();                                        // 부팅 직후 1회 재생
}

void loop() {
  if (digitalRead(BTN)==LOW) { delay(50); melody(); delay(300); }
  delay(20);
}
