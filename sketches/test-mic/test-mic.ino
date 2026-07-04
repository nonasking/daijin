// test-mic — INMP441 녹음 테스트 (임시 press-fit 검증용)
// BOOT 버튼 누르면 4초 녹음 → 맥으로 POST → whisper가 받아쓰기 → 마이크 작동 확인
// LED: 파랑깜빡=WiFi연결 / 초록=지금 말하세요(녹음) / 노랑=전송 / 초록3번=성공 / 빨강=실패
#include <ESP_I2S.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include "secrets.h"          // WIFI_SSID, WIFI_PASS, MAC_IP

#define RGB_PIN 48
#define BTN     0             // BOOT 버튼 (누르면 LOW)
#define I2S_SCK 2             // INMP441 SCK (진단 스윕으로 확정)
#define I2S_WS  1             // INMP441 WS
#define I2S_SD  42            // INMP441 SD (데이터)

I2SClass I2S;
inline void led(uint8_t r,uint8_t g,uint8_t b){ neopixelWrite(RGB_PIN,r,g,b); }

void setup() {
  Serial.begin(115200);
  pinMode(BTN, INPUT_PULLUP);
  led(20,20,20);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status()!=WL_CONNECTED){ led(0,0,20); delay(150); led(0,0,0); delay(150); }
  Serial.printf("WiFi OK %s\n", WiFi.localIP().toString().c_str());

  I2S.setPins(I2S_SCK, I2S_WS, -1, I2S_SD, -1);   // bclk, ws, dout(-1), din, mclk(-1)
  // INMP441은 24비트 데이터를 32비트 프레임으로 보냄 → 32비트로 받아 상위 16비트만 사용
  if (!I2S.begin(I2S_MODE_STD, 16000, I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO, I2S_STD_SLOT_LEFT)) {
    Serial.println("I2S begin 실패"); led(40,0,0); while(1) delay(1000);
  }
  led(0,30,0); delay(400); led(0,0,0);            // ready
  Serial.println("BOOT 버튼 누르면 4초 녹음합니다.");
}

void postWav(uint8_t* wav, size_t n) {
  HTTPClient http;
  String url = String("http://") + MAC_IP + ":8849/mic";
  http.begin(url);
  http.addHeader("Content-Type", "audio/wav");
  int code = http.POST(wav, n);
  Serial.printf("POST %s -> %d\n", url.c_str(), code);
  http.end();
  if (code>0 && code<300) { for(int i=0;i<3;i++){ led(0,40,0);delay(150);led(0,0,0);delay(150);} }
  else led(40,0,0);
}

// 4초 = 16000Hz × 4s × 16bit 모노
#define REC_SAMPLES (16000*4)
static uint8_t wavbuf[44 + REC_SAMPLES*2];

// 표준 PCM WAV 헤더 44바이트
void wavHeader(uint8_t* h, uint32_t nsamples) {
  uint32_t dlen = nsamples*2, flen = dlen+36, sr = 16000, br = sr*2;
  memcpy(h,"RIFF",4); memcpy(h+4,&flen,4); memcpy(h+8,"WAVEfmt ",8);
  uint32_t fmtlen=16; uint16_t pcm=1, ch=1, block=2, bits=16;
  memcpy(h+16,&fmtlen,4); memcpy(h+20,&pcm,2); memcpy(h+22,&ch,2);
  memcpy(h+24,&sr,4); memcpy(h+28,&br,4); memcpy(h+32,&block,2); memcpy(h+34,&bits,2);
  memcpy(h+36,"data",4); memcpy(h+40,&dlen,4);
}

void loop() {
  if (digitalRead(BTN)==LOW) {
    delay(60);
    led(0,40,0);                                   // 초록 = 지금 말하세요
    Serial.println("녹음 시작(4초)...");
    int16_t* pcm = (int16_t*)(wavbuf+44);
    static int32_t raw[1024];
    size_t got = 0;
    while (got < REC_SAMPLES) {
      size_t nb = I2S.readBytes((char*)raw, sizeof(raw));
      for (size_t i=0; i<nb/4 && got<REC_SAMPLES; i++) {
        int32_t v = raw[i] >> 13;                  // 상위 16비트 + x8 게인
        if (v>32767) v=32767; else if (v<-32768) v=-32768;
        pcm[got++] = (int16_t)v;
      }
    }
    wavHeader(wavbuf, REC_SAMPLES);
    led(40,40,0);                                  // 노랑 = 전송
    Serial.printf("녹음 %u bytes\n", (unsigned)sizeof(wavbuf));
    postWav(wavbuf, sizeof(wavbuf));
    delay(800); led(0,0,0);
  }
  delay(20);
}
