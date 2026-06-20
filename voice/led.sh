#!/bin/bash
# LED 제어 헬퍼 — Claude(음성비서)가 호출. 색을 HiveMQ 클라우드로 publish → 보드 LED 변경.
# 사용법: bash led.sh <색>   (red green blue yellow cyan magenta white off)
COLOR="${1:-off}"
SEC="$HOME/esp32-iot/secrets.local.txt"
H=$(grep '^MQTT_CLOUD_HOST=' "$SEC" 2>/dev/null | cut -d= -f2-)
PORT=$(grep '^MQTT_CLOUD_PORT=' "$SEC" 2>/dev/null | cut -d= -f2-)
U=$(grep '^MQTT_CLOUD_USER=' "$SEC" 2>/dev/null | cut -d= -f2-)
P=$(grep '^MQTT_CLOUD_PASS=' "$SEC" 2>/dev/null | cut -d= -f2-)
if mosquitto_pub -h "$H" -p "$PORT" --cafile /etc/ssl/cert.pem -u "$U" -P "$P" -t home/led/set -m "$COLOR"; then
  echo "OK: LED -> $COLOR (cloud)"
else
  echo "FAIL: 클라우드 브로커 전송 실패"; exit 1
fi
