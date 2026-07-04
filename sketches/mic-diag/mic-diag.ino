// mic-diag — INMP441 배선 자동 진단
// GPIO 1·2·42에 어떤 순서로 꽂혔든, WS/SCK/SD 6가지 조합 × 좌우 슬롯을 전부 훑어
// 신호가 잡히는 조합을 시리얼로 보고한다. (WiFi 불필요, 계속 반복)
#include <ESP_I2S.h>

#define RGB_PIN 48
// USB CDC(Serial)와 UART0(Serial0) 어느 포트로 꽂아도 보이도록 양쪽 출력
void logf(const char* fmt, ...) {
  char b[160]; va_list a; va_start(a, fmt);
  vsnprintf(b, sizeof(b), fmt, a); va_end(a);
  Serial.print(b); Serial0.print(b);
}
I2SClass I2S;
// {WS, SCK, SD} 후보 조합
const uint8_t perm[6][3] = {{1,2,42},{1,42,2},{2,1,42},{2,42,1},{42,1,2},{42,2,1}};
static int32_t buf[4096];

void testCfg(uint8_t ws, uint8_t sck, uint8_t sd, bool right) {
  I2S.setPins(sck, ws, -1, sd, -1);              // bclk, ws, dout(-1), din, mclk(-1)
  if (!I2S.begin(I2S_MODE_STD, 16000, I2S_DATA_BIT_WIDTH_32BIT,
                 I2S_SLOT_MODE_MONO, right ? I2S_STD_SLOT_RIGHT : I2S_STD_SLOT_LEFT)) {
    logf("WS=%2d SCK=%2d SD=%2d %s | begin fail\n", ws, sck, sd, right?"R":"L");
    return;
  }
  I2S.readBytes((char*)buf, sizeof(buf));        // 첫 버퍼는 버림 (안정화)
  size_t nb = I2S.readBytes((char*)buf, sizeof(buf));
  long mx = 0; size_t nz = 0, ns = nb/4;
  for (size_t i = 0; i < ns; i++) {
    int32_t v = buf[i] >> 16;                    // 상위 16비트
    if (v) nz++;
    long av = labs((long)v); if (av > mx) mx = av;
  }
  logf("WS=%2d SCK=%2d SD=%2d %s | max=%6ld nz=%4u/%u\n",
                ws, sck, sd, right?"R":"L", mx, (unsigned)nz, (unsigned)ns);
  I2S.end();
}

void setup() {
  Serial.begin(115200); Serial0.begin(115200);
  neopixelWrite(RGB_PIN, 20, 20, 20);            // 흰색 = 진단 중
  delay(2500);
}

void loop() {
  logf("=== 스윕 시작 (마이크에 계속 말해주세요) ===\n");
  for (int p = 0; p < 6; p++) {
    testCfg(perm[p][0], perm[p][1], perm[p][2], false);
    testCfg(perm[p][0], perm[p][1], perm[p][2], true);
  }
  logf("\n");
  delay(1000);
}
