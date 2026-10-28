// ota.h — 펌웨어를 HTTPS 로 받아 SHA-256 으로 검증하고 다른 앱 슬롯에 써서 재부팅한다. 14-daijin 과 15-home-node 가 같은 파일을 쓴다
// (Arduino 는 스케치 폴더 밖을 못 보니 복사본이다: tests/test_firmware_copies.py 가 둘이 같은지 본다).
//
// 명령:  ota:<url>:<sha256 hex 64자>      url 에 ':' 가 있으니 마지막 ':' 로 나눈다. sketches/ota.sh 가 만든다.
// 믿는 근거: 해시는 TLS+비밀번호로 인증된 MQTT 로 오고, 받은 바이트가 그 해시와 다르면 버린다. 내려받기 자체도 HTTPS(ISRG Root X1).
// 메모리: TLS 세션 하나가 40KB 쯤 먹는다. 다이진 기기는 힙이 30KB 대라 MQTT 의 TLS 를 먼저 끊고(otaRun 전에 호출자가) 받는다.
//         그래서 결과는 MQTT 로 바로 못 알린다: otaResult 에 남겨 두고, 다시 붙은 뒤 호출자가 state/ack 로 올린다. 성공하면 재부팅이 답이다.
// 파티션: 기본 esp32s3 스킴(app0/app1 각 1280KB). 스케치가 그 크기를 넘으면 Update.begin 이 거절한다.
#pragma once
#include <HTTPClient.h>
#include <Update.h>
#include <mbedtls/sha256.h>

#ifndef FW_VERSION
#define FW_VERSION "dev"              // ota.sh / flash-node.sh 가 -DFW_VERSION="<git>-<yymmddHHMM>" 으로 준다. status 의 fw 로 보인다
#endif

String otaResult;                     // 마지막 시도의 결과. 빈 문자열 = 알릴 것 없음

static bool otaRun(const String& url, const String& sha, const char* ca) {
  WiFiClientSecure c; c.setCACert(ca); c.setTimeout(30);
  HTTPClient http; http.setTimeout(30000); http.setReuse(false);
  if (!http.begin(c, url)) { otaResult = "ota:err:url"; return false; }
  int code = http.GET();
  if (code != 200) { otaResult = "ota:err:http:" + String(code); http.end(); return false; }
  int len = http.getSize();
  if (len <= 0) { otaResult = "ota:err:size"; http.end(); return false; }
  if (!Update.begin(len)) { otaResult = "ota:err:space:" + String(Update.errorString()); http.end(); return false; }
  Serial.printf("[ota] %d bytes <- %s\n", len, url.c_str());
  mbedtls_sha256_context ctx; mbedtls_sha256_init(&ctx); mbedtls_sha256_starts(&ctx, 0);
  WiFiClient* s = http.getStreamPtr();
  static uint8_t buf[2048]; size_t got = 0; uint32_t lastData = millis();
  while (got < (size_t)len) {
    size_t avail = s->available();
    if (!avail) {
      if (!s->connected() || millis() - lastData > 30000) break;
      delay(1); continue;
    }
    size_t n = s->read(buf, avail < sizeof(buf) ? avail : sizeof(buf));
    if (!n) continue;
    lastData = millis();
    if (Update.write(buf, n) != n) { otaResult = "ota:err:write:" + String(Update.errorString()); Update.abort(); http.end(); return false; }
    mbedtls_sha256_update(&ctx, buf, n); got += n;
  }
  http.end();
  unsigned char d[32]; mbedtls_sha256_finish(&ctx, d); mbedtls_sha256_free(&ctx);
  char hex[65]; for (int i = 0; i < 32; i++) sprintf(hex + 2 * i, "%02x", d[i]);
  if (got != (size_t)len) { otaResult = "ota:err:short:" + String(got) + "/" + String(len); Update.abort(); return false; }
  if (sha != String(hex)) { otaResult = "ota:err:sha-mismatch"; Update.abort(); return false; }
  if (!Update.end(true)) { otaResult = "ota:err:end:" + String(Update.errorString()); return false; }
  Serial.println("[ota] ok, rebooting");
  delay(300); ESP.restart();
  return true;
}

// "ota:<url>:<sha>" 를 풀어 otaRun. ota 명령이 아니면 false. 호출자는 그 전에 MQTT 를 끊어 두는 편이 안전하다 (위 메모리).
static bool otaCommand(const String& cmd, const char* ca) {
  if (!cmd.startsWith("ota:")) return false;
  int k = cmd.lastIndexOf(':');
  String url = k > 4 ? cmd.substring(4, k) : "", sha = k > 4 ? cmd.substring(k + 1) : "";
  sha.toLowerCase();
  if (!url.startsWith("https://") || sha.length() != 64) { otaResult = "ota:err:format"; return true; }
  otaRun(url, sha, ca);
  return true;
}
