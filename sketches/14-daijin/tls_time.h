// TLS 인증서 검증에는 대략 맞는 현재 시각이 필요하다. NTP(UDP 123)는 LTE 핫스팟에서 자주 막힌다.
// 그럴 때 검증을 끄는 대신 HTTP 응답의 Date 헤더(TCP 80)로 시계를 맞춘다.
//
// 이 시각은 인증되지 않은 값이다. 그래도 공격자가 시각을 속여 얻는 것은 "만료된 인증서를 유효해 보이게 하는 것"뿐이고,
// 여전히 CA 가 서명한 그 호스트의 인증서가 있어야 한다. 반대로 검증을 끄면(setInsecure) 같은 망의 누구든
// 브로커 행세를 하며 MQTT 비밀번호를 받아 갈 수 있고, 그 비밀번호는 브레인에게 명령을 내리는 열쇠다.
//
// 14-daijin 과 15-home-node 에 같은 파일이 들어 있다 (ca_cert.h 처럼 스케치마다 한 벌).
#pragma once
#include <WiFi.h>
#include <time.h>
#include <sys/time.h>

inline bool clockIsSet() { return time(nullptr) > 1700000000; }      // 2023-11 이후면 맞춰진 것으로 본다

inline bool syncNtp(uint32_t timeoutMs) {
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  uint32_t t0 = millis();
  while (!clockIsSet() && millis() - t0 < timeoutMs) delay(200);
  return clockIsSet();
}

// "Date: Fri, 02 Oct 2026 10:11:12 GMT" → epoch. 형식이 다르면 0.
inline time_t parseHttpDate(const char* line) {
  char mon[4] = {0}; int d, y, h, mi, s;
  if (sscanf(line, "%*[^,], %d %3s %d %d:%d:%d", &d, mon, &y, &h, &mi, &s) != 6) return 0;
  static const char names[] = "JanFebMarAprMayJunJulAugSepOctNovDec";
  const char* p = strstr(names, mon);
  if (!p || (p - names) % 3) return 0;
  int m = (p - names) / 3 + 1;
  // days_from_civil (그레고리력 → 1970-01-01 부터의 날 수). mktime 은 TZ 설정에 기대므로 쓰지 않는다.
  y -= m <= 2;
  long era = (y >= 0 ? y : y - 399) / 400;
  unsigned yoe = (unsigned)(y - era * 400);
  unsigned doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
  unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
  long days = era * 146097L + (long)doe - 719468L;
  return (time_t)days * 86400 + h * 3600 + mi * 60 + s;
}

inline bool syncHttpDate(const char* host, uint32_t timeoutMs) {
  WiFiClient c;
  if (!c.connect(host, 80, timeoutMs)) return false;
  c.setTimeout(timeoutMs);
  c.printf("HEAD / HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n", host);
  uint32_t t0 = millis();
  bool ok = false;
  while (millis() - t0 < timeoutMs) {
    String line = c.readStringUntil('\n');
    if (line.length() <= 1) break;                                    // 빈 줄 = 헤더 끝 (또는 시간 초과)
    if (!line.startsWith("Date:") && !line.startsWith("date:")) continue;
    time_t t = parseHttpDate(line.c_str());
    if (t > 1700000000) {
      struct timeval tv = { t, 0 };
      settimeofday(&tv, nullptr);
      ok = true;
    }
    break;
  }
  c.stop();
  return ok && clockIsSet();
}

// 시계를 어디서 맞췄는지 (로그용). 이미 맞아 있어서 아무것도 안 했으면 처음 값 그대로다.
static const char* clockSource = "이미 맞음";

// NTP 를 먼저, 안 되면 HTTP Date 를 몇 군데에 물어본다. 전부 안 되면 false.
// 시험용: -DTLS_TIME_SKIP_NTP 로 빌드하면 NTP 를 건너뛰고 HTTP Date 경로만 탄다 (NTP 가 막힌 망을 흉내).
//   시계는 USB 리셋을 넘어 유지되므로(RTC), 이 빌드는 부팅 때 시계를 한 번 지우고 시작한다.
inline bool syncClock(uint32_t ntpMs = 12000) {
#ifdef TLS_TIME_SKIP_NTP
  static bool cleared = false;
  if (!cleared) { struct timeval z = {0, 0}; settimeofday(&z, nullptr); cleared = true; }
#endif
  if (clockIsSet()) return true;
#ifndef TLS_TIME_SKIP_NTP
  if (syncNtp(ntpMs)) { clockSource = "NTP"; return true; }
#endif
  static const char* hosts[] = {"www.google.com", "www.cloudflare.com", "www.apple.com"};
  for (const char* h : hosts) if (syncHttpDate(h, 5000)) { clockSource = "HTTP Date"; return true; }
  return false;
}
