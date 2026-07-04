// 14-daijin — AI 음성 대화 친구 디바이스 (Phase 2)
// BOOT 누르면 4초 녹음 → PCM 청크로 HiveMQ 발행(daijin/audio/in)
// 브레인 답(daijin/audio/out PCM 청크)을 받는 대로 스피커로 스트리밍 재생
// 프로토콜: docs/chunking-protocol.md — [clipId:1][seq:2 BE][flags:1][PCM<=4096B]
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
const int   REC_SEC   = 4;
const size_t CHUNK    = 4096;                  // PCM 페이로드 (0.128초)
const size_t HDR      = 4;

WiFiMulti wifiMulti;      // 홈 WiFi + 아이폰 핫스팟 중 잡히는 곳 자동 접속
WiFiClientSecure tls;
PubSubClient mqtt(tls);
I2SClass i2sMic, i2sSpk;
uint8_t clipId = 0;
bool playing = false;

inline void led(uint8_t r,uint8_t g,uint8_t b){ neopixelWrite(RGB_PIN,r,g,b); }
#define LED_IDLE led(0,5,0)                     // 은은한 초록 = 대기(살아있음)

void chirp() {                                  // "띠링" = 준비 완료 (LTE에선 연결에 30초+ 걸려 소리로 알림)
  static int16_t b[800];
  const float fr[2] = {660.f, 990.f};
  for (int f = 0; f < 2; f++) {
    for (int i = 0; i < 800; i++) b[i] = (int16_t)(6000 * sinf(2 * PI * fr[f] * i / 16000.f));
    i2sSpk.write((uint8_t*)b, sizeof(b)); i2sSpk.write((uint8_t*)b, sizeof(b));
  }
  static int16_t z[256] = {0}; i2sSpk.write((uint8_t*)z, sizeof(z));
}
void logf(const char* fmt, ...) {
  char b[160]; va_list a; va_start(a, fmt);
  vsnprintf(b, sizeof(b), fmt, a); va_end(a);
  Serial.print(b); Serial0.print(b);
}

// ---------- 재생: 링버퍼(지터 버퍼) — 0.75초 쌓이면 재생 시작 ----------
// 네트워크 도착 간격이 출렁여도 버퍼가 흡수해 끊김 없는 재생
#define RING_SZ  65536                          // 2초분
#define PREBUF   24576                          // 0.75초 차면 재생 시작
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

void ringPush16(int16_t s) {
  while (ringFree() < 2) ringPlayChunk();       // 가득 차면 재생으로 비움 (블로킹=실시간)
  size_t off = rw % RING_SZ;
  ring[off] = s & 0xFF; ring[(off + 1) % RING_SZ] = (s >> 8) & 0xFF;
  rw += 2;
}

void onMqtt(char* topic, byte* payload, unsigned int len) {
  if (strcmp(topic, T_AUDIO_OUT) != 0 || len <= HDR) return;
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
// 고정 4초의 문제(말 시작 전 낭비·끝 잘림 → STT 오인식) 해결. 최소 1초, 최대 10초 자동컷.
void recordAndPublish() {
  while (digitalRead(BTN) == LOW) delay(10);                   // 시작 탭에서 손 뗄 때까지
  delay(80);                                                   // 디바운스
  WiFi.setSleep(false);                                        // 오디오 세션 시작 → 풀속도
  led(0,40,0);                                                 // 초록 = 녹음 중 (말하세요)
  logf("녹음+전송 시작 (clip %u, 탭-토글)\n", clipId);
  static int32_t raw[1024];
  static uint8_t msg[HDR + CHUNK];
  const size_t minS = SR * 1, maxS = SR * 10;
  size_t sent = 0, fill = 0; uint16_t seq = 0;
  bool done = false;
  while (!done) {
    size_t nb = i2sMic.readBytes((char*)raw, sizeof(raw));
    for (size_t i = 0; i < nb/4; i++) {
      int32_t v = raw[i] >> 15;                                // x2 게인 (클리핑 방지)
      if (v > 32767) v = 32767; else if (v < -32768) v = -32768;
      int16_t s = (int16_t)v;
      memcpy(msg + HDR + fill, &s, 2); fill += 2; sent++;
      bool stopTap = (digitalRead(BTN) == LOW && sent >= minS);// 종료 탭
      done = stopTap || sent >= maxS;
      if (fill == CHUNK || done) {
        msg[0] = clipId; msg[1] = seq >> 8; msg[2] = seq & 0xFF; msg[3] = done ? 1 : 0;
        if (!mqtt.publish(T_AUDIO_IN, msg, HDR + fill)) logf("청크 %u 발행 실패\n", seq);
        mqtt.loop();
        seq++; fill = 0;
        if (done) break;
      }
    }
  }
  led(40,40,0);                                                // 노랑 = 전송 마무리
  logf("전송 완료: %u 청크 (%.1f초)\n", seq, sent / (float)SR);
  clipId++;
  while (digitalRead(BTN) == LOW) delay(10);                   // 종료 탭 손 뗄 때까지
  delay(120);                                                  // 디바운스 (재시작 방지)
  LED_IDLE;
}

// ---------- 연결 ----------
void mqttConnect() {
  while (!mqtt.connected()) {
    led(30,0,30);                                              // 보라 = MQTT 연결 중
    String cid = "daijin-dev-" + String((uint32_t)ESP.getEfuseMac(), HEX);
    if (mqtt.connect(cid.c_str(), MQTT_USER, MQTT_PASS)) {
      mqtt.subscribe(T_AUDIO_OUT, 0);   // QoS0: ACK 게이팅 없이 TCP 스트리밍
      logf("MQTT 연결 OK\n");
      led(0,30,0); delay(200); LED_IDLE;
    } else {
      logf("MQTT 실패 rc=%d, 3초 후 재시도\n", mqtt.state());
      delay(3000);
    }
  }
}

void setup() {
  Serial.begin(115200); Serial0.begin(115200);
  pinMode(BTN, INPUT_PULLUP);
  led(20,20,20);

  wifiMulti.addAP(WIFI_SSID, WIFI_PASS);      // 집
  wifiMulti.addAP(WIFI_SSID2, WIFI_PASS2);    // 아이폰 핫스팟 (집 밖)
  while (wifiMulti.run() != WL_CONNECTED) { led(0,0,20); delay(150); led(0,0,0); delay(150); }
  // 스마트 절전: 평소 ON(발열·배터리 절약), 녹음·재생 순간에만 OFF (절전시 수신 ~12KB/s로 제한됨)
  WiFi.setSleep(true);
  logf("WiFi OK %s (%s)\n", WiFi.localIP().toString().c_str(), WiFi.SSID().c_str());

  // TLS: NTP 시각 동기(12초 제한) 성공 시 CA 검증, 실패 시 setInsecure 폴백 (13-cloud-led 패턴)
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  time_t now = 0; int waited = 0;
  while (now < 8 * 3600 && waited < 12000) { delay(250); waited += 250; now = time(nullptr); }
  if (now >= 8 * 3600) { tls.setCACert(ISRG_ROOT_X1); logf("TLS: CA 검증 모드\n"); }
  else { tls.setInsecure(); logf("TLS: NTP 실패 → insecure 폴백\n"); }

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMqtt);
  mqtt.setBufferSize(HDR + CHUNK + 128);                       // 수신 청크 수용
  mqtt.setKeepAlive(60); mqtt.setSocketTimeout(30);

  // 마이크: 32비트 프레임 필수 (INMP441)
  i2sMic.setPins(MIC_SCK, MIC_WS, -1, MIC_SD, -1);
  if (!i2sMic.begin(I2S_MODE_STD, SR, I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO, I2S_STD_SLOT_LEFT)) {
    logf("마이크 I2S 실패\n"); led(40,0,0); while(1) delay(1000);
  }
  // 스피커: 16비트 TX
  i2sSpk.setPins(SPK_BCK, SPK_LRC, SPK_DIN, -1, -1);
  if (!i2sSpk.begin(I2S_MODE_STD, SR, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO)) {
    logf("스피커 I2S 실패\n"); led(40,0,0); while(1) delay(1000);
  }

  mqttConnect();
  chirp();                                       // 준비 완료 알림음
  LED_IDLE;
  logf("daijin v3(QoS0+계측) 준비 완료 — BOOT 누르고 말하세요\n");
}

// 60초마다 자기 상태(칩온도·WiFi·업타임) retained 발행 — 브레인이 status.sh로 읽음
void publishStatus() {
  static uint32_t last = 0;
  if (millis() - last < 60000 && last != 0) return;
  last = millis();
  char js[160];
  snprintf(js, sizeof(js),
    "{\"temp_c\":%.1f,\"ssid\":\"%s\",\"rssi\":%d,\"uptime_s\":%lu,\"heap\":%u}",
    temperatureRead(), WiFi.SSID().c_str(), WiFi.RSSI(),
    (unsigned long)(millis()/1000), (unsigned)ESP.getFreeHeap());
  mqtt.publish(T_STATUS, (const uint8_t*)js, strlen(js), true);   // retained
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {        // 망 이동(집↔핫스팟) 시 재접속
    led(0,0,20); wifiMulti.run(); WiFi.setSleep(true); LED_IDLE;
  }
  if (!mqtt.connected()) mqttConnect();
  mqtt.loop();
  publishStatus();

  // 재생 상태 머신: 프리버퍼 차거나 마지막 청크 오면 시작, 다 비우면 종료
  if (!playing && (ringAvail() >= PREBUF || (gotLast && ringAvail() > 0))) {
    playing = true; led(0,30,30);               // 청록 = 재생
  }
  if (playing) {
    if (ringAvail() == 0 && !gotLast) {         // 버퍼 고갈 = 언더런 (끊김 지점)
      if (!inUnderrun) { inUnderrun = true; underruns++; }
    } else inUnderrun = false;
    ringPlayChunk();
    if (gotLast && ringAvail() == 0) {
      static int16_t z[512] = {0};
      i2sSpk.write((uint8_t*)z, sizeof(z));     // 팝 방지 무음
      playing = false; gotLast = false; LED_IDLE;
      WiFi.setSleep(true);                      // 오디오 세션 끝 → 절전 복귀
      logf("재생 완료 · 언더런 %u회\n", underruns);
    }
  }

  if (digitalRead(BTN) == LOW && !playing) {
    delay(60);
    recordAndPublish();
  }
  if (!playing) delay(10);                      // 재생 중엔 지연 없이 돌기
}
