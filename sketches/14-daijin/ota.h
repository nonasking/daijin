// ota.h — 펌웨어를 HTTPS 로 받아 SHA-256 으로 검증하고 다른 앱 슬롯에 써서 재부팅한다. 14-daijin 과 15-home-node 가 같은 파일을 쓴다
// (Arduino 는 스케치 폴더 밖을 못 보니 복사본이다: tests/test_firmware_copies.py 가 둘이 같은지 본다).
//
// 명령:  ota:<url>:<sha256 hex 64자>      url 에 ':' 가 있으니 마지막 ':' 로 나눈다. sketches/ota.sh 가 만든다.
// 믿는 근거: 해시는 받은 바이트가 그 해시와 같은지만 본다. 해시가 명령과 같은 메시지로 오니 출처는 증명하지 못한다(2026-11-01 리뷰).
//   그래서 출처는 두 겹으로 묶는다. (1) URL 은 우리 서버의 /fw/ 로만 (OTA_URL_PREFIX, 컴파일할 때 정한다): cmd 에 쓸 수 있는
//   누군가가 자기 서버의 바이너리를 가리켜도 받지 않는다. (2) 클라우드 우체국에 붙어 있을 때만: 집 우체국은 평문이고 ACL 이 없다.
//   cmd 토픽에 쓸 수 있는 것은 brain 뿐이다 (voice/mosquitto/daijin.acl).
// 되돌리기: 새 펌웨어는 "확인 대기"로 부팅한다 (verifyRollbackLater). 클라우드 우체국에 붙으면 otaConfirm 이 확인하고,
//   OTA_CONFIRM_MS 안에 못 붙거나 그 전에 죽으면(부트로더가 확인 대기 상태로 다시 켜지는 걸 보고) 예전 펌웨어로 돌아간다.
//   펌웨어를 고쳤는데 브로커 비밀번호나 인증서를 잘못 넣었을 때 USB 를 꺼내지 않아도 되게 하려는 것.
// 메모리: TLS 세션 하나가 40KB 쯤 먹는다. 다이진 기기는 힙이 30KB 대라 MQTT 의 TLS 를 먼저 끊고(otaRun 전에 호출자가) 받는다.
//         그래서 결과는 MQTT 로 바로 못 알린다: otaResult 에 남겨 두고, 다시 붙은 뒤 호출자가 state/ack 로 올린다. 성공하면 재부팅이 답이다.
// 파티션: 기본 esp32s3 스킴(app0/app1 각 1280KB). 스케치가 그 크기를 넘으면 Update.begin 이 거절한다.
#pragma once
#include <HTTPClient.h>
#include <Update.h>
#include <esp_ota_ops.h>
#include <mbedtls/sha256.h>

#ifndef FW_VERSION
#define FW_VERSION "dev"              // ota.sh / flash-node.sh 가 -DFW_VERSION="<git>-<yymmddHHMM>" 으로 준다. status 의 fw 로 보인다
#endif
#ifndef OTA_URL_PREFIX
#define OTA_URL_PREFIX "https://" MQTT_HOST "/fw/"    // 브로커와 같은 서버의 nginx (voice/ota/install-server.sh)
#endif
#ifndef OTA_CONFIRM_MS
#define OTA_CONFIRM_MS (5UL * 60 * 1000)              // 새 펌웨어가 이 안에 클라우드 우체국에 붙지 못하면 되돌린다
#endif

String otaResult;                     // 마지막 시도의 결과. 빈 문자열 = 알릴 것 없음

// 코어의 약한 함수를 덮는다: 부팅하자마자 "정상"으로 표시하지 말고, otaConfirm 이 확인할 때까지 기다린다.
// extern "C" 가 꼭 있어야 한다: 코어(esp32-hal-misc.c)는 C 이름으로 찾는다. 없으면 C++ 이름으로 따로 만들어져 아무도 부르지 않고,
// 코어의 기본값(false: 부팅하자마자 정상)이 그대로 쓰인다 (2026-11-02 첫 배포에서 실제로 그랬다: 펌웨어의 nm 에 W 하나뿐).
extern "C" bool verifyRollbackLater() { return true; }

static bool otaPending() {
  esp_ota_img_states_t st;
  return esp_ota_get_state_partition(esp_ota_get_running_partition(), &st) == ESP_OK && st == ESP_OTA_IMG_PENDING_VERIFY;
}

// 클라우드 우체국에 붙은 직후 부른다. 확인 대기 중인 새 펌웨어를 "정상"으로 표시한다. 그 밖에는 아무 일도 안 한다.
// USB 로 올린 펌웨어는 대기 상태가 아니므로 해당 없다.
static void otaConfirm(bool onCloud) {
  if (!onCloud || !otaPending()) return;
  esp_ota_mark_app_valid_cancel_rollback();
  Serial.printf("[ota] %s 확인: 클라우드 우체국에 붙었다\n", FW_VERSION);
  if (!otaResult.length()) otaResult = "ota:ok:" FW_VERSION;
}

// loop() 에서 부른다. 새 펌웨어가 OTA_CONFIRM_MS 안에 확인을 받지 못했으면 예전 펌웨어로 되돌리고 재부팅한다.
static void otaWatch() {
  static bool checked = false, pending = false;
  if (!checked) { pending = otaPending(); checked = true; }
  if (!pending) return;
  if (!otaPending()) { pending = false; return; }       // 그새 확인됐다
  if (millis() < OTA_CONFIRM_MS) return;
  Serial.println("[ota] 새 펌웨어가 우체국에 못 붙었다 → 예전 펌웨어로 되돌린다");
  delay(200);
  esp_ota_mark_app_invalid_rollback_and_reboot();
}

static bool otaRun(const String& url, const String& sha, const char* ca) {
  WiFiClientSecure c; c.setCACert(ca);
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
    int n = s->read(buf, avail < sizeof(buf) ? avail : sizeof(buf));
    if (n < 0) { otaResult = "ota:err:read"; Update.abort(); http.end(); return false; }
    if (n == 0) continue;
    lastData = millis();
    if (Update.write(buf, n) != (size_t)n) { otaResult = "ota:err:write:" + String(Update.errorString()); Update.abort(); http.end(); return false; }
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

// "ota:<url>:<sha>" 를 풀어 otaRun. ota 명령이 아니면 false. onCloud 가 아니면(집 우체국) 받지 않는다.
// 호출자는 그 전에 MQTT 를 끊어 두는 편이 안전하다 (위 메모리). 거절은 MQTT 를 끊기 전에 알 수 있게 otaAccepts 로 먼저 볼 것.
static bool otaAccepts(const String& cmd, bool onCloud, String* why = nullptr) {
  int k = cmd.lastIndexOf(':');
  String url = k > 4 ? cmd.substring(4, k) : "", sha = k > 4 ? cmd.substring(k + 1) : "";
  const char* err = nullptr;
  if (!onCloud) err = "ota:err:not-on-cloud";
  else if (sha.length() != 64) err = "ota:err:format";
  else if (!url.startsWith(OTA_URL_PREFIX) || url.indexOf("..") >= 0) err = "ota:err:url-not-ours";
  if (err && why) *why = err;
  return err == nullptr;
}

static bool otaCommand(const String& cmd, const char* ca, bool onCloud) {
  if (!cmd.startsWith("ota:")) return false;
  if (!otaAccepts(cmd, onCloud, &otaResult)) return true;
  int k = cmd.lastIndexOf(':');
  String sha = cmd.substring(k + 1); sha.toLowerCase();
  otaRun(cmd.substring(4, k), sha, ca);
  return true;
}
