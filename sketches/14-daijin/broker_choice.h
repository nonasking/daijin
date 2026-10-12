// 우체국 고르기: 평소엔 클라우드(HiveMQ, TLS), 클라우드에 못 붙으면 집의 우체국(mosquitto, 같은 와이파이)으로.
// 인터넷이 통째로 끊겨도 기기·노드·맥의 브레인이 집 안에서 계속 말을 주고받게 하려는 것.
//
// secrets.h 에 MQTT_LOCAL_HOST 를 넣었을 때만 켜진다 (넣지 않으면 지금까지와 똑같이 클라우드만 쓴다):
//   #define MQTT_LOCAL_HOST "my-mac.local"   // 집 mosquitto 가 도는 기계: 이름(.local, mDNS) 또는 고정 IP
//                                            // 이름을 권한다: 공유기가 IP 를 바꿔도 따라간다 (2026-10-12 실제로 .143 → .189)
//   #define MQTT_LOCAL_PORT 1883
//   #define MQTT_LOCAL_USER "iot"
//   #define MQTT_LOCAL_PASS "..."
// 동작: 시각을 못 얻어 인증서를 검증할 수 없거나 클라우드 접속이 3번 연달아 실패하면 집 우체국으로 간다.
//       집 우체국에 있는 동안 5분마다 클라우드를 다시 시도하고, 붙으면 돌아간다.
// 집 우체국은 같은 와이파이 안이라 암호화 없이(평문) 붙는다. 그 망을 믿을 수 있을 때만 쓸 것.
//
// 14-daijin 과 15-home-node 에 같은 파일이 들어 있다 (tls_time.h 처럼 스케치마다 한 벌).
#pragma once
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <PubSubClient.h>
#include "tls_time.h"
#if defined(MQTT_LOCAL_HOST)
#include <ESPmDNS.h>
#endif

#if defined(MQTT_LOCAL_HOST)
#define HAS_LOCAL_BROKER 1
#ifndef MQTT_LOCAL_PORT
#define MQTT_LOCAL_PORT 1883
#endif
#else
#define HAS_LOCAL_BROKER 0
#endif

struct BrokerChoice {
  WiFiClientSecure* tls = nullptr;
  WiFiClient plain;
  PubSubClient* mqtt = nullptr;
  bool onLocal = false;
  bool tlsReady = false;            // 클라우드용 인증서 검증 준비가 끝났나 (시각을 얻었나)
  uint8_t cloudFails = 0;
  uint32_t localSince = 0;
  bool resolveAgain = false;        // 집 우체국 접속이 실패했으면 다음 시도 전에 이름을 다시 찾는다
  static const uint8_t CLOUD_FAILS_TO_LOCAL = 3;
  static const uint32_t RETRY_CLOUD_MS = 5UL * 60 * 1000;

  void begin(WiFiClientSecure& t, PubSubClient& m, bool clockReady) {
    tls = &t; mqtt = &m; tlsReady = clockReady;
    if (tlsReady) tls->setCACert(ISRG_ROOT_X1);
    useCloud();
  }
  bool enabled() const { return HAS_LOCAL_BROKER; }
  const char* name() const { return onLocal ? "local" : "cloud"; }
  const char* user() const {
#if HAS_LOCAL_BROKER
    if (onLocal) return MQTT_LOCAL_USER;
#endif
    return MQTT_USER;
  }
  const char* pass() const {
#if HAS_LOCAL_BROKER
    if (onLocal) return MQTT_LOCAL_PASS;
#endif
    return MQTT_PASS;
  }
  void useCloud() {
    onLocal = false;
    mqtt->setClient(*tls);
    mqtt->setServer(MQTT_HOST, MQTT_PORT);
  }
  void useLocal() {
#if HAS_LOCAL_BROKER
    onLocal = true; localSince = millis(); resolveAgain = false;
    mqtt->setClient(plain);
    IPAddress ip;
    if (resolveLocal(ip)) mqtt->setServer(ip, MQTT_LOCAL_PORT);
    else mqtt->setServer(MQTT_LOCAL_HOST, MQTT_LOCAL_PORT);   // IP 로 적었거나 못 찾았으면 그대로 (못 찾으면 접속이 실패하고 다시 찾는다)
#endif
  }
#if HAS_LOCAL_BROKER
  // "이름.local" 이면 mDNS 로 지금의 IP 를 찾는다.
  static bool resolveLocal(IPAddress& out) {
    String host = MQTT_LOCAL_HOST;
    if (!host.endsWith(".local")) return false;
    static bool mdnsUp = false;
    if (!mdnsUp) {
      char me[24]; snprintf(me, sizeof me, "daijin-%06llx", ESP.getEfuseMac() & 0xFFFFFF);
      mdnsUp = MDNS.begin(me);
    }
    if (!mdnsUp) return false;
    out = MDNS.queryHost(host.substring(0, host.length() - 6), 2000);
    Serial.printf("집 우체국 %s → %s\n", MQTT_LOCAL_HOST, out == IPAddress() ? "못 찾음" : out.toString().c_str());
    return out != IPAddress();
  }
#endif
  // 접속 시도 직전에 부른다: 어느 우체국으로 갈지 정한다. 바뀌었으면 true.
  bool choose() {
    if (!HAS_LOCAL_BROKER) return false;
    if (onLocal) {
      if (millis() - localSince < RETRY_CLOUD_MS) {
        if (resolveAgain) { uint32_t since = localSince; useLocal(); localSince = since; }
        return false;
      }
      if (!tlsReady && syncClock(3000)) { tls->setCACert(ISRG_ROOT_X1); tlsReady = true; }
      localSince = millis();                       // 실패해도 다음 재시도는 5분 뒤
      if (!tlsReady) return false;
      cloudFails = CLOUD_FAILS_TO_LOCAL - 1;      // 한 번 더 실패하면 바로 집으로 돌아온다
      useCloud(); return true;
    }
    if (!tlsReady || cloudFails >= CLOUD_FAILS_TO_LOCAL) { useLocal(); return true; }
    return false;
  }
  // 접속 시도 결과를 알려 준다.
  // 시험용: -DBROKER_TEST_CLOUD_DOWN 으로 빌드하면 클라우드 접속이 늘 실패한 것으로 친다 (인터넷을 끊지 않고 폴백을 보려고).
  // 돌려주는 값이 실제로 붙었는지다 (시험 빌드에서는 클라우드 접속을 끊고 false).
  bool result(bool ok) {
#ifdef BROKER_TEST_CLOUD_DOWN
    if (!onLocal) { cloudFails = 255; if (ok) mqtt->disconnect(); return false; }
#endif
    if (onLocal) { resolveAgain = !ok; return ok; }
    cloudFails = ok ? 0 : (cloudFails < 255 ? cloudFails + 1 : 255);
    return ok;
  }
};
