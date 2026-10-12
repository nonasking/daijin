// 14-daijin — AI 음성 대화 친구 디바이스 (Phase 2)
// BOOT 을 한 번 누르면 녹음 시작. 말이 끝나면(0.7초 무음) 알아서 끝나고, 한 번 더 눌러도 끝 (최소 1초, 12초에서 자동 컷)
// → IMA ADPCM 청크로 HiveMQ 발행(daijin/audio/in)
// 브레인 답(daijin/audio/out ADPCM 청크)을 받는 대로 지터 버퍼를 거쳐 스피커로 스트리밍 재생
// 프로토콜: docs/chunking-protocol.md — [clipId:1][seq:2 BE][flags:1 (bit0=마지막, bit1=ADPCM)][ADPCM<=2048B]
//   헤더만 있는 마지막 청크 = "답할 것 없음" (브레인이 무음·환청으로 버린 클립): 짧게 알리고 대기로 돌아간다
//
// 배선(검증 완료):
//   INMP441  마이크: WS=1, SCK=2, SD=42, L/R=GND, VDD=3.3V  (32비트 프레임 필수)
//   Audio Converter & Amplifier: BCK=14, LCK=12, DIN=13, VCC=5V, SCK=미연결, 스피커=L+/L-
// LED: 파랑깜빡=WiFi / 보라=MQTT연결 / 초록=녹음(말하세요) / 노랑=업로드 / 청록=재생 / 빨강=오류
#include <WiFi.h>
#include <WiFiMulti.h>
#include <WiFiClientSecure.h>
#include <PubSubClient.h>
#include <ESP_I2S.h>
#include <time.h>
#include "secrets.h"     // WIFI_SSID/PASS, MQTT_HOST/PORT/USER/PASS
#include "ca_cert.h"     // ISRG Root X1 (Let's Encrypt)
#include "tls_time.h"    // 시계 맞추기: NTP, 막히면 HTTP Date (인증서 검증을 끄지 않기 위해)
#include "broker_choice.h" // 클라우드에 못 붙으면 집 우체국으로 (secrets.h 에 MQTT_LOCAL_HOST 가 있을 때만)

// 시각을 끝내 못 맞췄을 때 인증서 검증 없이라도 붙을지. 0(기본) = 붙지 않고 시각이 잡힐 때까지 계속 시도한다.
// 1 로 빌드하면 예전처럼 검증 없이 붙는다: 같은 망의 누구든 브로커 행세로 MQTT 비밀번호를 가로챌 수 있으니 급할 때만.
#ifndef ALLOW_INSECURE_TLS
#define ALLOW_INSECURE_TLS 0
#endif
#include "face_types.h"  // OLED 얼굴 상태 enum

#define RGB_PIN 48
#define BTN     0
// 마이크 (INMP441)
#define MIC_WS  1
#define MIC_SCK 2
#define MIC_SD  42
// 스피커 (앰프 모듈)
#define SPK_LRC 12
#define SPK_DIN 13
#define SPK_BCK 14

#define T_AUDIO_IN  "daijin/audio/in"
#define T_AUDIO_OUT "daijin/audio/out"
#define T_STATUS    "daijin/status"

const int   SR        = 16000;
const size_t CHUNK    = 4096;                  // 수신 페이로드 상한이자 I2S 로 한 번에 쓰는 PCM 크기 (0.128초)
const size_t HDR      = 4;

WiFiMulti wifiMulti;      // 홈 WiFi + 아이폰 핫스팟 중 잡히는 곳 자동 접속
WiFiClientSecure tls;
PubSubClient mqtt(tls);
BrokerChoice broker;
I2SClass i2sMic, i2sSpk;
uint8_t clipId = 0;
bool playing = false;
bool statusDue = false;   // true 면 다음 loop 에서 상태를 바로 발행 (접속 직후)

inline void led(uint8_t r,uint8_t g,uint8_t b){ neopixelWrite(RGB_PIN,r,g,b); }
#define LED_IDLE led(0,5,0)                     // 은은한 초록 = 대기(살아있음)

// ---------- 얼굴: SSD1306 128x64 I2C OLED (SDA=8, SCL=9, 주소 0x3C) ----------
// 파이프라인 단계를 표정으로 보여준다. OLED가 안 꽂혀 있으면 자동으로 건너뛴다(hasFace=false).
// 그리기는 상태 전환 때와 애니메이션 틱(150~400ms)에서만: I2C 1KB 전송 ≈ 25ms라 오디오에 영향 없음.
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#define OLED_SDA 8
#define OLED_SCL 9
Adafruit_SSD1306 oled(128, 64, &Wire, -1);
bool hasFace = false;
Face faceState = F_IDLE;
uint32_t faceSince = 0, faceTickAt = 0;
bool faceFrame = false;

void drawEye(int cx, int cy, int w, int h, int px, int py) {   // 눈(흰 타원) + 눈동자(검정)
  oled.fillRoundRect(cx - w/2, cy - h/2, w, h, h/3, SSD1306_WHITE);
  if (h > 6) oled.fillCircle(cx + px, cy + py, h/6 + 2, SSD1306_BLACK);
}
void drawFace() {
  if (!hasFace) return;
  oled.clearDisplay();
  int lx = 38, rx = 90, ey = 26;
  switch (faceState) {
    case F_IDLE:                                              // 평소: 가끔 깜빡
      if (faceFrame) { oled.fillRect(lx-14, ey-1, 28, 3, SSD1306_WHITE); oled.fillRect(rx-14, ey-1, 28, 3, SSD1306_WHITE); }
      else { drawEye(lx, ey, 28, 22, 0, 0); drawEye(rx, ey, 28, 22, 0, 0); }
      oled.fillRoundRect(54, 48, 20, 4, 2, SSD1306_WHITE);   // 다문 입
      break;
    case F_LISTEN:                                            // 듣는 중: 눈 크게, 입 살짝 벌림
      drawEye(lx, ey, 34, 30, 0, 0); drawEye(rx, ey, 34, 30, 0, 0);
      oled.drawRoundRect(52, 46, 24, 10, 4, SSD1306_WHITE);
      break;
    case F_UPLOAD:                                            // 보내는 중: 위를 봄
      drawEye(lx, ey, 28, 22, 0, -4); drawEye(rx, ey, 28, 22, 0, -4);
      oled.fillRoundRect(54, 48, 20, 4, 2, SSD1306_WHITE);
      break;
    case F_THINK: {                                           // 생각 중: 눈동자 좌우로
      int px = faceFrame ? 6 : -6;
      drawEye(lx, ey, 28, 22, px, -2); drawEye(rx, ey, 28, 22, px, -2);
      oled.fillRoundRect(50, 48, 28, 3, 1, SSD1306_WHITE);
      for (int i = 0; i < 3; i++) oled.fillCircle(104 + i*7, 58, 2, (i == ((millis()/300)%3)) ? SSD1306_WHITE : SSD1306_BLACK);
      break; }
    case F_SPEAK:                                             // 말하는 중: 입이 열렸다 닫혔다
      drawEye(lx, ey, 28, 22, 0, 0); drawEye(rx, ey, 28, 22, 0, 0);
      if (faceFrame) oled.fillRoundRect(50, 44, 28, 14, 6, SSD1306_WHITE);
      else           oled.fillRoundRect(52, 48, 24, 5, 2, SSD1306_WHITE);
      break;
    case F_ERROR:                                             // 오류: X 눈
      for (int x : {lx, rx}) { oled.drawLine(x-10, ey-10, x+10, ey+10, SSD1306_WHITE); oled.drawLine(x-10, ey+10, x+10, ey-10, SSD1306_WHITE); }
      oled.drawRoundRect(52, 48, 24, 8, 3, SSD1306_WHITE);
      break;
  }
  oled.display();
}
void face(Face f) { if (faceState != f) { faceState = f; faceSince = millis(); faceFrame = false; faceTickAt = 0; drawFace(); } }
void faceTick() {                                             // loop()에서 호출: 상태별 애니메이션
  if (!hasFace) return;
  uint32_t now = millis(), period;
  switch (faceState) {
    case F_IDLE:  period = faceFrame ? 120 : 2600 + (now % 1500); break;   // 깜빡: 짧게 감고 오래 뜸
    case F_THINK: period = 400; break;
    case F_SPEAK: period = 160; break;
    default:      period = 1000; break;
  }
  if (now - faceTickAt < period) return;
  faceTickAt = now; faceFrame = !faceFrame;
  if (faceState == F_IDLE || faceState == F_THINK || faceState == F_SPEAK) drawFace();
  if (faceState == F_THINK && now - faceSince > 90000) face(F_IDLE);      // 답이 영영 안 오면 원래 얼굴로
}
void faceInit() {
  Wire.begin(OLED_SDA, OLED_SCL);
  Wire.beginTransmission(0x3C);
  if (Wire.endTransmission() != 0) { logf("OLED 없음 (얼굴 생략)\n"); return; }
  if (!oled.begin(SSD1306_SWITCHCAPVCC, 0x3C)) { logf("OLED 초기화 실패\n"); return; }
  hasFace = true; oled.setRotation(0); face(F_IDLE); logf("OLED 얼굴 OK\n");
}

// ---------- 알림음 ----------
// 톤 버퍼 하나(50ms)를 같이 쓰고 무음은 플래시의 0 배열을 쓴다. 알림음마다 버퍼를 따로 두던 때보다 RAM 을 8KB 가까이 덜 쓴다
// (이 보드는 TLS 재접속에 힙이 빠듯하다: 아래 CAP_SZ 주석 참고).
// 주파수는 50ms 에 정수 주기가 들어가는 값(20Hz 의 배수)을 쓴다: 버퍼를 이어 붙일 때 틱 소리가 안 난다.
static int16_t toneBuf[800];
static const int16_t ZEROS[256] = {0};
void tone16(float hz, int amp, int ms) {
  for (int i = 0; i < 800; i++) toneBuf[i] = (int16_t)(amp * sinf(2 * PI * hz * i / 16000.f));
  for (int k = 0; k < ms / 50; k++) i2sSpk.write((uint8_t*)toneBuf, sizeof(toneBuf));
}
void hush(int ms) {                             // 무음 (팝 방지, 음 사이 간격)
  for (int n = ms * 16; n > 0; n -= 256) i2sSpk.write((uint8_t*)ZEROS, (n < 256 ? n : 256) * 2);
}
void chirp() {                                  // "띠링" = 준비 완료 (LTE에선 연결에 30초+ 걸려 소리로 알림)
  tone16(660.f, 6000, 100); tone16(980.f, 6000, 100); hush(16);
}
void cutChirp() {                               // "뚜-뚜" 낮게 두 번 = 12초 자동컷. 이 소리 들리면 이미 전송됐으니 버튼 누르지 말 것
  for (int r = 0; r < 2; r++) { tone16(440.f, 5000, 100); hush(100); }
}
void softChirp(float hz, int ms) {              // 낮은 단음: 320Hz 짧게 = "답할 것 없음", 220Hz 길게 = 전송 실패
  tone16(hz, 4000, ms); hush(16);
}
void logf(const char* fmt, ...) {
  char b[160]; va_list a; va_start(a, fmt);
  vsnprintf(b, sizeof(b), fmt, a); va_end(a);
  Serial.print(b); Serial0.print(b);
}

// ---------- 재생: 링버퍼(지터 버퍼) — 0.4초 쌓이면 재생 시작 ----------
// 네트워크 도착 간격이 출렁여도 버퍼가 흡수해 끊김 없는 재생
#define RING_SZ  65536                          // 2초분
#define PREBUF   6400                           // 0.2초 차면 재생 시작 (ADPCM 8KB/s vs 회선 ~19KB/s라 여유; 0.75→0.4→0.2로 체감 지연 단축. 끊기면 12800 으로)
static uint8_t ring[RING_SZ];
static volatile size_t rw = 0, rr = 0;          // 누적 write/read 바이트
static volatile bool gotLast = false;
// 계측: 수신 속도·언더런 진단용
static uint32_t rxT0 = 0, rxT1 = 0, underruns = 0, rxBytes = 0;
static bool inUnderrun = false;
inline size_t ringAvail() { return rw - rr; }
inline size_t ringFree()  { return RING_SZ - ringAvail(); }

void ringPlayChunk() {                          // 링에서 최대 CHUNK만큼 I2S로
  size_t n = ringAvail(); if (!n) return;
  if (n > CHUNK) n = CHUNK;
  size_t off = rr % RING_SZ, tail = RING_SZ - off;
  if (n <= tail) i2sSpk.write(ring + off, n);
  else { i2sSpk.write(ring + off, tail); i2sSpk.write(ring, n - tail); }
  rr += n;
}

// ---------- IMA ADPCM 디코더 (4bit → 16bit PCM, 4:1 압축) ----------
// EU 브로커 RTT 300ms × ESP32 TCP창 5.7KB = 최대 ~19KB/s → PCM(32KB/s) 불가, ADPCM(8KB/s) 여유
static const int16_t ADPCM_IDX[8] = {-1,-1,-1,-1,2,4,6,8};
static const int16_t ADPCM_STEP[89] = {
  7,8,9,10,11,12,13,14,16,17,19,21,23,25,28,31,34,37,41,45,50,55,60,66,
  73,80,88,97,107,118,130,143,157,173,190,209,230,253,279,307,337,371,
  408,449,494,544,598,658,724,796,876,963,1060,1166,1282,1411,1552,1707,
  1878,2066,2272,2499,2749,3024,3327,3660,4026,4428,4871,5358,5894,6484,
  7132,7845,8630,9493,10442,11487,12635,13899,15289,16818,18500,20350,
  22385,24623,27086,29794,32767};
static int32_t adPred = 0; static int adIdx = 0;

inline int16_t adpcmNibble(uint8_t c) {
  int step = ADPCM_STEP[adIdx];
  int vp = step >> 3;
  if (c & 4) vp += step;
  if (c & 2) vp += step >> 1;
  if (c & 1) vp += step >> 2;
  adPred += (c & 8) ? -vp : vp;
  if (adPred > 32767) adPred = 32767; else if (adPred < -32768) adPred = -32768;
  adIdx += ADPCM_IDX[c & 7];
  if (adIdx < 0) adIdx = 0; else if (adIdx > 88) adIdx = 88;
  return (int16_t)adPred;
}

// ---------- IMA ADPCM 인코더 (16bit PCM → 4bit, 업스트림용) ----------
// 업로드도 같은 물리 한계(TCP 송신버퍼 5.7KB × RTT 300ms ≈ 19KB/s)에 걸려
// 원본 PCM(32KB/s)은 실시간 전송 불가 → 4:1 압축(8KB/s)으로 해결. 브레인 AdpcmEnc와 동일 알고리즘.
static int32_t encPred = 0; static int encIdx = 0; static int encLo = -1;

inline uint8_t adpcmEncode(int16_t s) {
  int diff = s - encPred;
  uint8_t code = 0;
  if (diff < 0) { code = 8; diff = -diff; }
  int step = ADPCM_STEP[encIdx];
  if (diff >= step)      { code |= 4; diff -= step; }
  if (diff >= step >> 1) { code |= 2; diff -= step >> 1; }
  if (diff >= step >> 2) { code |= 1; }
  int vp = step >> 3;
  if (code & 4) vp += step;
  if (code & 2) vp += step >> 1;
  if (code & 1) vp += step >> 2;
  encPred += (code & 8) ? -vp : vp;
  if (encPred > 32767) encPred = 32767; else if (encPred < -32768) encPred = -32768;
  encIdx += ADPCM_IDX[code & 7];
  if (encIdx < 0) encIdx = 0; else if (encIdx > 88) encIdx = 88;
  return code;
}

void ringPush16(int16_t s) {
  while (ringFree() < 2) ringPlayChunk();       // 가득 차면 재생으로 비움 (블로킹=실시간)
  size_t off = rw % RING_SZ;
  ring[off] = s & 0xFF; ring[(off + 1) % RING_SZ] = (s >> 8) & 0xFF;
  rw += 2;
}

void onMqtt(char* topic, byte* payload, unsigned int len) {
  if (strcmp(topic, T_AUDIO_OUT) != 0 || len < HDR) return;      // len == HDR: 페이로드 없는 청크 ("답할 것 없음" 신호)
  size_t n = len - HDR;
  if (payload[1] == 0 && payload[2] == 0) {     // seq 0 = 새 클립
    WiFi.setSleep(false);                       // 재생 세션 → 풀속도
    rw = rr = 0; gotLast = false; playing = false;
    adPred = 0; adIdx = 0;                      // ADPCM 상태 리셋 (브레인 인코더와 동기)
    rxT0 = millis(); underruns = 0; inUnderrun = false; rxBytes = 0;
  }
  rxBytes += n;
  if (payload[3] & 2) {                         // bit1 = ADPCM: 바이트당 니블 2개 디코드
    for (size_t i = 0; i < n; i++) {
      ringPush16(adpcmNibble(payload[HDR + i] & 0x0F));
      ringPush16(adpcmNibble(payload[HDR + i] >> 4));
    }
  } else {                                      // 비압축 PCM (구형 호환)
    for (size_t i = 0; i + 1 < n; i += 2)
      ringPush16((int16_t)(payload[HDR + i] | (payload[HDR + i + 1] << 8)));
  }
  if (payload[3] & 1) {
    gotLast = true;
    rxT1 = millis();
    uint32_t ms = rxT1 - rxT0;
    logf("수신 %uB(압축) / %ums = %uB/s (필요 8000B/s)\n",
         (unsigned)rxBytes, ms, ms ? (unsigned)(rxBytes * 1000UL / ms) : 0);
  }
}

// ---------- 녹음(탭-토글): BOOT 한 번 = 시작, 한 번 더 = 종료·전송 ----------
// 고정 4초의 문제(말 시작 전 낭비·끝 잘림 → STT 오인식) 해결. 최소 1초, 최대 12초 자동컷 (REC_MIN_B / REC_MAX_B).
//
// 캡처 태스크 분리 (2026-08-16): I2SClass의 DMA 버퍼는 6×240프레임 ≈ 90ms 고정이라,
// 같은 루프에서 TLS 발행(수십~수백 ms 블로킹)을 하면 그동안의 샘플이 유실됐다
// (증상: 음절 사이가 뚝뚝 잘림 → STT 오인식). 캡처는 전용 태스크가 쉬지 않고
// DMA를 비워 0.75초 링에 쌓고, 메인은 링에서 꺼내 발행만 한다. 재생 지터 버퍼와 대칭.
#define CAP_SZ 24576                                 // 캡처 링 0.75초분 — 48KB는 TLS 재핸드셰이크 힙(~45KB)을 고갈시켜 재접속 불능(rc=-2)
static uint8_t capRing[CAP_SZ];
static volatile size_t capW = 0, capR = 0;           // 누적 쓰기/읽기 (SPSC)
static volatile bool capturing = false;
static volatile uint32_t capDrops = 0;
inline size_t capAvail() { return capW - capR; }

// 녹음을 끝내는 판정은 캡처 태스크가 한다 (2026-10-03).
// 예전엔 전송 루프가 청크 사이사이에 버튼을 읽었는데, 전송이 네트워크에 막혀 있는 동안의 짧은 탭을 놓쳤다
// (증상: 종료 탭을 했는데도 12초 자동컷까지 녹음 → 긴 무음이 붙어 STT 가 문장 끝을 흘림).
// 캡처 태스크는 전송과 상관없이 32ms 마다 돌고, 그보다 짧은 눌림은 인터럽트가 잡아 둔다.
const size_t REC_MIN_B = SR * 1 * 2;                 // 최소 1초: 그 전의 눌림(시작 탭의 떨림 포함)은 버린다
const size_t REC_MAX_B = SR * 12 * 2;                // 최대 12초 자동컷 (PCM 바이트)
static volatile bool btnEdge = false;                // BOOT 이 눌리는 순간 인터럽트가 세운다
static volatile bool stopTap = false;                // 녹음이 탭으로 끝났다
void IRAM_ATTR onBtnDown() { btnEdge = true; }

// ---------- 발화 끝 자동 감지 (에너지 VAD, 2026-10-08) ----------
// "말이 끝나고 탭하기까지의 공백"을 없앤다. 캡처 태스크가 32ms 블록마다 RMS 를 재고 바닥 소음을 따라간다.
// 말소리 블록(바닥의 4배이자 VAD_MIN_SPEECH 이상)이 누적 300ms 를 넘긴 뒤, 700ms 동안 조용하면(바닥의 2.5배 아래) 녹음을 끝낸다.
// 바닥은 말소리가 아닌 블록에서만 갱신한다 (말하는 동안 바닥이 따라 올라가 문장 중간에 끊기는 일을 막는다).
// 최소 1초·최대 12초·탭 종료는 그대로. 시끄러운 곳에서 못 끝내면 탭하면 되고, -DVAD_AUTO_STOP=0 으로 끌 수 있다.
// 아직 실기 보정 전: 로그의 "말 %ums · 바닥 %.0f · 최고 %d" 로 문턱을 맞춘다. (브레인 쪽 실측: 발화 상위창 RMS 1500~9000)
#ifndef VAD_AUTO_STOP
#define VAD_AUTO_STOP 1
#endif
const uint32_t VAD_SPEECH_MS  = 300;
const uint32_t VAD_SILENCE_MS = 700;
const float    VAD_MIN_SPEECH = 400.f;               // 이보다 조용한 블록은 말소리로 보지 않는다 (x2 게인 기준)
static volatile bool stopVad = false;                // 녹음이 무음 감지로 끝났다
static uint32_t vadSpeechMs = 0, vadSilenceMs = 0;
static float vadFloor = 0; static int vadPeak = 0;

void vadReset() { stopVad = false; vadSpeechMs = vadSilenceMs = 0; vadFloor = 0; vadPeak = 0; }
void vadBlock(float rms, uint32_t ms) {
  float onTh  = fmaxf(VAD_MIN_SPEECH, vadFloor * 4.f);
  float offTh = fmaxf(VAD_MIN_SPEECH * 0.6f, vadFloor * 2.5f);
  if (vadFloor <= 0.f || rms < vadFloor) vadFloor = rms;                 // 바닥은 바로 내려가고
  else if (rms < onTh) vadFloor += (rms - vadFloor) * 0.05f;             // 말소리가 아닐 때만 천천히 올라간다
  if (rms > onTh) { vadSpeechMs += ms; vadSilenceMs = 0; if (rms > vadPeak) vadPeak = (int)rms; }
  else if (rms < offTh) vadSilenceMs += ms;
}

void captureTask(void*) {
  static int32_t raw[512];
  while (capturing) {
    size_t nb = i2sMic.readBytes((char*)raw, sizeof(raw));
    size_t n = nb / 4; double sumsq = 0;
    for (size_t i = 0; i < n; i++) {
      int32_t v = raw[i] >> 15;                      // x2 게인 (클리핑 방지)
      if (v > 32767) v = 32767; else if (v < -32768) v = -32768;
      int16_t s = (int16_t)v;
      sumsq += (double)s * s;
      if (CAP_SZ - capAvail() >= 2) {
        size_t off = capW % CAP_SZ;
        capRing[off] = s & 0xFF; capRing[(off + 1) % CAP_SZ] = (s >> 8) & 0xFF;
        capW += 2;
      } else capDrops++;                             // 링 포화 (발행이 0.75초 이상 정체)
    }
    if (n) vadBlock(sqrtf((float)(sumsq / n)), (uint32_t)(n * 1000 / SR));
    if (capW < REC_MIN_B) btnEdge = false;
    else if (btnEdge || digitalRead(BTN) == LOW) { stopTap = true; capturing = false; }   // 종료 탭
#if VAD_AUTO_STOP
    else if (vadSpeechMs >= VAD_SPEECH_MS && vadSilenceMs >= VAD_SILENCE_MS) { stopVad = true; capturing = false; }   // 말 끝
#endif
    if (capW >= REC_MAX_B) capturing = false;                                             // 자동컷
  }
  vTaskDelete(NULL);
}

void recordAndPublish() {
  while (digitalRead(BTN) == LOW) delay(10);                   // 시작 탭에서 손 뗄 때까지
  delay(80);                                                   // 디바운스
  WiFi.setSleep(false);                                        // 오디오 세션 시작 → 풀속도
  led(0,40,0);                                                 // 초록 = 녹음 중 (말하세요)
  face(F_LISTEN);
  logf("녹음+전송 시작 (clip %u, 탭-토글)\n", clipId);
  const size_t CHUNK_UP = 2048;                                // ADPCM 청크 (=4096샘플=256ms)
  static uint8_t msg[HDR + CHUNK];
  capW = capR = 0; capDrops = 0; stopTap = false; btnEdge = false; vadReset(); capturing = true;
  encPred = 0; encIdx = 0; encLo = -1;                         // 인코더 리셋 (브레인 디코더와 동기)
  xTaskCreatePinnedToCore(captureTask, "cap", 4096, NULL, 10, NULL, 0);
  size_t fill = 0; uint16_t seq = 0;
  bool lastSent = false, shown = false;
  uint16_t sendFails = 0;
  while (!lastSent) {
    if (!capturing && !shown) {                                // 캡처가 끝났다(탭 또는 자동컷): 바로 표시해서 탭이 먹혔음을 알린다
      shown = true; led(40,40,0); face(F_UPLOAD);              // 노랑 = 남은 청크 전송 중
    }
    if (capAvail() < 2 && capturing) { delay(5); continue; }   // 캡처 대기
    while (capAvail() >= 2 && fill < CHUNK_UP) {               // PCM 2바이트 → ADPCM 니블
      size_t off = capR % CAP_SZ;
      int16_t s = (int16_t)(capRing[off] | (capRing[(off + 1) % CAP_SZ] << 8));
      capR += 2;
      uint8_t code = adpcmEncode(s);
      if (encLo < 0) encLo = code;
      else { msg[HDR + fill++] = encLo | (code << 4); encLo = -1; }
    }
    bool last = (!capturing && capAvail() < 2);                // 링까지 다 비움 = 마지막
    if (last && encLo >= 0) { msg[HDR + fill++] = (uint8_t)encLo; encLo = -1; }  // 홀수 니블
    if (fill == CHUNK_UP || last) {
      msg[0] = clipId; msg[1] = seq >> 8; msg[2] = seq & 0xFF;
      msg[3] = (last ? 1 : 0) | 2;                             // bit1 = ADPCM
      if (!mqtt.publish(T_AUDIO_IN, msg, HDR + fill)) { sendFails++; logf("청크 %u 발행 실패\n", seq); }
      mqtt.loop();
      seq++; fill = 0;
      lastSent = last;
    }
  }
  bool autoCut = !stopTap && !stopVad;                         // 탭도 무음 감지도 아니면 12초 상한에 걸린 것
  led(40,40,0);                                                // 노랑 = 전송 마무리
  face(F_UPLOAD);
  if (capDrops) logf("경고: 캡처 링 포화로 %u 샘플 유실\n", (unsigned)capDrops);
  logf("전송 완료: %u 청크 (%.1f초, %s) · 말 %ums · 바닥 %.0f · 최고 %d\n", seq, (capW / 2) / (float)SR,
       stopTap ? "탭으로 종료" : (stopVad ? "무음 감지" : "자동컷"), (unsigned)vadSpeechMs, vadFloor, vadPeak);
  clipId++;
  if (stopVad) {
    // 무음 감지로 끝났으면 사용자가 습관처럼 "종료" 탭을 할 수 있다. 짧은 확인음을 내고,
    // 자동컷과 같은 방식으로 버튼이 1.5초 떼어져 있을 때까지 새 녹음을 받지 않는다.
    softChirp(660.f, 100);
    uint32_t quiet = millis();
    while (millis() - quiet < 1500) { if (digitalRead(BTN) == LOW) quiet = millis(); delay(10); }
  } else if (autoCut) {
    // 자동컷: 사용자는 아직 말하는 중이라 곧 "종료" 탭을 누른다. 그 탭이 새 녹음 시작으로 읽히면
    // 스피커 소리까지 10초 더 녹음돼 헛명령이 된다(2026-09-15 실측: 클립 3·4, 5·6, 8·9 쌍).
    // → 알림음 내고, 버튼이 1.5초 동안 계속 떼어져 있을 때까지 새 녹음을 받지 않는다.
    cutChirp();
    uint32_t quiet = millis();
    while (millis() - quiet < 1500) { if (digitalRead(BTN) == LOW) quiet = millis(); delay(10); }
    logf("자동컷 → 1.5초 쿨다운 통과\n");
  } else {
    while (digitalRead(BTN) == LOW) delay(10);                 // 종료 탭 손 뗄 때까지
    delay(120);                                                // 디바운스 (재시작 방지)
  }
  LED_IDLE;
  if (sendFails) {                                             // 녹음 도중 연결이 끊겼다: 브레인은 이 클립을 못 받았으니 답을 기다리지 않는다
    logf("전송 실패 %u 청크 → 답을 기다리지 않음\n", sendFails);
    softChirp(220.f, 300);
    face(F_IDLE);
    WiFi.setSleep(true);
  } else {
    face(F_THINK);                                             // 답이 올 때까지 생각하는 얼굴
  }
}

// ---------- 연결 ----------
// 한 번 시도하고 돌아온다. 실패하면 3초 뒤 loop() 가 다시 부른다.
// 예전엔 여기서 붙을 때까지 돌았는데, 그 안에서는 WiFi 를 다시 찾지 않아서 망을 옮기면(집 ↔ 핫스팟) 그대로 갇혔다.
// LWT: 전원이 뽑히거나 망이 끊기면 브로커가 status 에 offline 을 retained 로 남긴다 (홈 노드와 같은 방식).
const char* WILL_OFFLINE = "{\"online\":false}";
bool mqttTry() {
  static uint32_t lastTry = 0;
  if (mqtt.connected()) return true;
  if (lastTry && millis() - lastTry < 3000) return false;
  lastTry = millis();
  led(30,0,30);                                                // 보라 = MQTT 연결 중
  if (broker.choose()) logf("우체국 바꿈 → %s\n", broker.name());
  String cid = "daijin-dev-" + String((uint32_t)ESP.getEfuseMac(), HEX);
  bool ok = mqtt.connect(cid.c_str(), broker.user(), broker.pass(), T_STATUS, 1, true, WILL_OFFLINE);
  ok = broker.result(ok);
  if (ok) {
    mqtt.subscribe(T_AUDIO_OUT, 0);     // QoS0: ACK 게이팅 없이 TCP 스트리밍
    logf("MQTT 연결 OK (%s)\n", broker.name());
    statusDue = true;                   // 접속 즉시 online 상태를 올려 LWT 의 offline 을 덮는다
    led(0,30,0); delay(200); LED_IDLE;
    return true;
  }
  logf("MQTT 실패 rc=%d, 3초 후 재시도\n", mqtt.state());
  return false;
}

// 시계가 맞아야 인증서를 검증할 수 있다. NTP → HTTP Date 순으로 시도 (tls_time.h).
// 집 우체국이 설정돼 있으면 시각을 못 얻어도 기다리지 않는다: 인터넷이 끊긴 것이니 집 우체국으로 간다 (broker_choice.h).
void setupTls() {
  if (syncClock()) { broker.begin(tls, mqtt, true); logf("TLS: CA 검증 모드 (시각: %s)\n", clockSource); return; }
  if (broker.enabled()) { broker.begin(tls, mqtt, false); logf("TLS: 시각 동기 실패 → 집 우체국으로 시작 (5분마다 클라우드 재시도)\n"); return; }
  broker.begin(tls, mqtt, false);
#if ALLOW_INSECURE_TLS
  tls.setInsecure(); logf("TLS: 시각 동기 실패 → 검증 없이 접속 (ALLOW_INSECURE_TLS=1)\n");
#else
  logf("TLS: 시각 동기 실패 → 검증을 끄지 않고 계속 시도\n");
  while (!syncClock(4000)) {                                   // 빨강 깜빡 = 시각을 못 얻는 중 (인터넷이 안 되는 망)
    led(40,0,0); delay(300); led(0,0,0); delay(700);
    if (WiFi.status() != WL_CONNECTED) wifiMulti.run();
  }
  tls.setCACert(ISRG_ROOT_X1); logf("TLS: CA 검증 모드 (시각: %s)\n", clockSource);
#endif
}

void setup() {
  Serial.begin(115200); Serial0.begin(115200);
  pinMode(BTN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(BTN), onBtnDown, FALLING);   // 녹음 종료 탭을 놓치지 않게 (captureTask 가 읽는다)
  led(20,20,20);
  faceInit();                                    // OLED 있으면 얼굴, 없으면 조용히 생략

  // WIFI_ONLY=1(집)|2(핫스팟) 빌드 플래그로 한 망만 등록 가능. 촬영 때 메인은 집 와이파이에 고정해야
  // 노드들과 다른 망이 된다 (2026-09-15 실측: 플래그 없이는 신호 센 핫스팟에 붙어 dev1과 같은 망이 됨).
#if !defined(WIFI_ONLY) || WIFI_ONLY == 1
  wifiMulti.addAP(WIFI_SSID, WIFI_PASS);      // 집
#endif
#if !defined(WIFI_ONLY) || WIFI_ONLY == 2
  wifiMulti.addAP(WIFI_SSID2, WIFI_PASS2);    // 아이폰 핫스팟 (집 밖)
#endif
  while (wifiMulti.run() != WL_CONNECTED) { led(0,0,20); delay(150); led(0,0,0); delay(150); }
  // 스마트 절전: 평소 ON(발열·배터리 절약), 녹음·재생 순간에만 OFF (절전시 수신 ~12KB/s로 제한됨)
  WiFi.setSleep(true);
  logf("WiFi OK %s (%s)\n", WiFi.localIP().toString().c_str(), WiFi.SSID().c_str());

  setupTls();                                    // 우체국(클라우드/집)도 여기서 정해진다

  mqtt.setCallback(onMqtt);
  mqtt.setBufferSize(HDR + CHUNK + 128);                       // 수신 청크 수용
  mqtt.setKeepAlive(60); mqtt.setSocketTimeout(30);

  // 마이크: 32비트 프레임 필수 (INMP441)
  i2sMic.setPins(MIC_SCK, MIC_WS, -1, MIC_SD, -1);
  if (!i2sMic.begin(I2S_MODE_STD, SR, I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO, I2S_STD_SLOT_LEFT)) {
    logf("마이크 I2S 실패\n"); led(40,0,0); face(F_ERROR); while(1) delay(1000);
  }
  // 스피커: 16비트 TX
  i2sSpk.setPins(SPK_BCK, SPK_LRC, SPK_DIN, -1, -1);
  if (!i2sSpk.begin(I2S_MODE_STD, SR, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO)) {
    logf("스피커 I2S 실패\n"); led(40,0,0); face(F_ERROR); while(1) delay(1000);
  }

  while (!mqttTry()) { delay(200); if (WiFi.status() != WL_CONNECTED) wifiMulti.run(); }
  chirp();                                       // 준비 완료 알림음
  LED_IDLE;
  logf("daijin v3(QoS0+계측) 준비 완료 — BOOT 누르고 말하세요\n");
}

// 60초마다(그리고 접속 직후) 자기 상태(칩온도·WiFi·업타임) retained 발행 — 브레인이 status.sh로 읽음.
// online 과 ts(epoch)를 같이 실어서, 꺼진 뒤에 남은 값이 현재 상태처럼 읽히지 않게 한다 (꺼지면 LWT 가 offline 으로 덮는다).
void publishStatus() {
  static uint32_t last = 0;
  if (!statusDue && millis() - last < 60000 && last != 0) return;
  statusDue = false;
  last = millis();
  char js[256];
  snprintf(js, sizeof(js),
    "{\"online\":true,\"ts\":%ld,\"temp_c\":%.1f,\"ssid\":\"%s\",\"rssi\":%d,\"uptime_s\":%lu,\"heap\":%u,\"broker\":\"%s\"}",
    (long)time(nullptr), temperatureRead(), WiFi.SSID().c_str(), WiFi.RSSI(),
    (unsigned long)(millis()/1000), (unsigned)ESP.getFreeHeap(), broker.name());
  mqtt.publish(T_STATUS, (const uint8_t*)js, strlen(js), true);   // retained
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {        // 망 이동(집↔핫스팟) 시 재접속: 아는 망 중 잡히는 곳으로
    led(0,0,20);
    if (wifiMulti.run() != WL_CONNECTED) { faceTick(); delay(300); return; }
    WiFi.setSleep(true); LED_IDLE;
    logf("WiFi 재접속 %s\n", WiFi.SSID().c_str());
  }
  if (!mqttTry()) { faceTick(); delay(50); return; }   // 브로커가 안 붙으면 이번 바퀴는 여기까지 (WiFi 확인부터 다시)
  mqtt.loop();
  publishStatus();
  faceTick();

  // 재생 상태 머신: 프리버퍼 차거나 마지막 청크 오면 시작, 다 비우면 종료
  if (!playing && (ringAvail() >= PREBUF || (gotLast && ringAvail() > 0))) {
    playing = true; led(0,30,30);               // 청록 = 재생
    face(F_SPEAK);
  }
  if (playing) {
    if (ringAvail() == 0 && !gotLast) {         // 버퍼 고갈 = 언더런 (끊김 지점)
      if (!inUnderrun) { inUnderrun = true; underruns++; }
    } else inUnderrun = false;
    ringPlayChunk();
    if (gotLast && ringAvail() == 0) {
      hush(32);                                 // 팝 방지 무음
      playing = false; gotLast = false; LED_IDLE; face(F_IDLE);
      WiFi.setSleep(true);                      // 오디오 세션 끝 → 절전 복귀
      logf("재생 완료 · 언더런 %u회\n", underruns);
    }
  }

  // "답할 것 없음": 헤더만 있는 마지막 청크를 받았다 (재생할 게 없는데 클립이 끝남)
  if (!playing && gotLast && ringAvail() == 0) {
    gotLast = false;
    softChirp(320.f, 150);
    LED_IDLE; face(F_IDLE);
    WiFi.setSleep(true);
    logf("답할 것 없음 신호 → 대기\n");
  }

  if (digitalRead(BTN) == LOW && !playing) {
    delay(60);
    recordAndPublish();
  }
  if (!playing) delay(10);                      // 재생 중엔 지연 없이 돌기
}
