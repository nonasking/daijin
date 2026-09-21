#!/bin/bash
# 능동 발화 헬퍼 — 다이진 스피커로 말하게 한다.
# 사용법: bash say.sh "안녕"                  → 그대로 말함 (daijin/say)
#         bash say.sh -a "dev2가 오프라인 됐어" → 에이전트가 스스로 표현해 말함 (daijin/ask)
TOPIC="daijin/say"
if [ "$1" = "-a" ]; then TOPIC="daijin/ask"; shift; fi
TEXT="${1:?말할 텍스트 필요}"
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
if mosquitto_pub -h "$H" -p "$PORT" "${CAFILE[@]}" -u "$U" -P "$P" \
     -t "$TOPIC" -m "$TEXT"; then
  echo "OK: $TOPIC <- $TEXT"
else
  echo "FAIL: 브로커 전송 실패"; exit 1
fi
