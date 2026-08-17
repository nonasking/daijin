#!/bin/bash
# 능동 발화 헬퍼 — 다이진 스피커로 말하게 한다.
# 사용법: bash say.sh "안녕"                  → 그대로 말함 (daijin/say)
#         bash say.sh -a "dev2가 오프라인 됐어" → 에이전트가 스스로 표현해 말함 (daijin/ask)
TOPIC="daijin/say"
if [ "$1" = "-a" ]; then TOPIC="daijin/ask"; shift; fi
TEXT="${1:?말할 텍스트 필요}"
SEC="$HOME/esp32-iot/secrets.local.txt"
H=$(grep '^MQTT_CLOUD_HOST=' "$SEC" 2>/dev/null | cut -d= -f2-)
PORT=$(grep '^MQTT_CLOUD_PORT=' "$SEC" 2>/dev/null | cut -d= -f2-)
U=$(grep '^MQTT_CLOUD_USER=' "$SEC" 2>/dev/null | cut -d= -f2-)
P=$(grep '^MQTT_CLOUD_PASS=' "$SEC" 2>/dev/null | cut -d= -f2-)
if mosquitto_pub -h "$H" -p "$PORT" --cafile /etc/ssl/cert.pem -u "$U" -P "$P" \
     -t "$TOPIC" -m "$TEXT"; then
  echo "OK: $TOPIC <- $TEXT"
else
  echo "FAIL: 브로커 전송 실패"; exit 1
fi
