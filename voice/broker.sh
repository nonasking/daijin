#!/bin/bash
# 헬퍼 스크립트 공통: 브로커 선택 + mosquitto_pub/sub 인증 인자. 각 스크립트가 source 해서 쓴다.
#   AUTH=(-h … -p … [--cafile …] -u … -P …)   로컬 mosquitto(1883)가 살아 있으면 로컬, 아니면 클라우드
#   H, PORT                                  선택된 브로커 (H=localhost 면 로컬 브리지 경유)
#   CLOUD_AUTH=(…)                           항상 클라우드 (console.sh처럼 원본을 봐야 할 때)
#   SEC                                      저장소 루트의 secrets.local.txt (클론 위치 무관)
# 로컬 브리지는 클라우드 직결(TLS 왕복 1.3초)보다 명령당 3초 이상 빠르다. 없으면 클라우드로 폴백.
# 맥·리눅스 공통: CA 번들 위치를 순서대로 찾고, 포트 감지는 bash 내장 /dev/tcp 로 (nc·timeout 의존 없음).
SEC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/secrets.local.txt"
_sec() { grep "^$1=" "$SEC" 2>/dev/null | cut -d= -f2-; }

CA=""
for _ca in /etc/ssl/cert.pem /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt; do
  if [ -f "$_ca" ]; then CA="$_ca"; break; fi          # macOS / Debian·Ubuntu / RHEL·Fedora
done
CLOUD_AUTH=(-h "$(_sec MQTT_CLOUD_HOST)" -p "$(_sec MQTT_CLOUD_PORT)")
if [ -n "$CA" ]; then CLOUD_AUTH+=(--cafile "$CA"); fi
CLOUD_AUTH+=(-u "$(_sec MQTT_CLOUD_USER)" -P "$(_sec MQTT_CLOUD_PASS)")

# 로컬은 connection refused 가 즉시 오므로 타임아웃 없이도 안전하다.
if (exec 3<>/dev/tcp/127.0.0.1/1883) 2>/dev/null; then
  H=localhost; PORT=1883
  AUTH=(-h localhost -p 1883 -u "$(_sec MQTT_USER)" -P "$(_sec MQTT_PASS)")
else
  H="$(_sec MQTT_CLOUD_HOST)"; PORT="$(_sec MQTT_CLOUD_PORT)"
  AUTH=("${CLOUD_AUTH[@]}")
fi
