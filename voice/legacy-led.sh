#!/bin/bash
# LED 제어 헬퍼 — Claude(음성비서)가 호출. 색을 HiveMQ 클라우드로 publish → 보드 LED 변경.
# [legacy] home/led/set 토픽은 11~13번 학습 스케치만 듣는다. 14-daijin/15-home-node에는 효과 없음.
# 현행 LED 제어: bash dev.sh all led:<색>  (또는 dev.sh 1번 led:red)
# 사용법: bash led.sh <색>   (red green blue yellow cyan magenta white off)
COLOR="${1:-off}"
SEC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/secrets.local.txt"   # 저장소 루트의 secrets (클론 위치 무관)
# 로컬 mosquitto(1883, 평문)로 붙는다 — HiveMQ 브리지가 클라우드와 동기화 (mosquitto.conf 참고).
# 클라우드 직결(TLS 왕복 1.3초)보다 명령당 3초 이상 빠르다. 로컬 브로커가 죽어 있으면 클라우드로 폴백.
H=localhost; PORT=1883
U=$(grep '^MQTT_USER=' "$SEC" 2>/dev/null | cut -d= -f2-)
P=$(grep '^MQTT_PASS=' "$SEC" 2>/dev/null | cut -d= -f2-)
CAFILE=()
if ! nc -z localhost 1883 2>/dev/null; then
  H=$(grep '^MQTT_CLOUD_HOST=' "$SEC" 2>/dev/null | cut -d= -f2-)
  PORT=$(grep '^MQTT_CLOUD_PORT=' "$SEC" 2>/dev/null | cut -d= -f2-)
  U=$(grep '^MQTT_CLOUD_USER=' "$SEC" 2>/dev/null | cut -d= -f2-)
  P=$(grep '^MQTT_CLOUD_PASS=' "$SEC" 2>/dev/null | cut -d= -f2-)
  CAFILE=(--cafile /etc/ssl/cert.pem)
fi
if mosquitto_pub -h "$H" -p "$PORT" "${CAFILE[@]}" -u "$U" -P "$P" -t home/led/set -m "$COLOR"; then
  echo "OK: LED -> $COLOR (cloud)"
else
  echo "FAIL: 클라우드 브로커 전송 실패"; exit 1
fi
