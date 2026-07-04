#!/bin/bash
# 디바이스 상태 조회 헬퍼 — Claude(daijin 두뇌)가 호출.
# 디바이스가 daijin/status 에 retained로 발행하는 JSON(온도·WiFi·업타임)을 읽어온다.
# 사용법: bash status.sh
SEC="$HOME/esp32-iot/secrets.local.txt"
H=$(grep '^MQTT_CLOUD_HOST=' "$SEC" 2>/dev/null | cut -d= -f2-)
PORT=$(grep '^MQTT_CLOUD_PORT=' "$SEC" 2>/dev/null | cut -d= -f2-)
U=$(grep '^MQTT_CLOUD_USER=' "$SEC" 2>/dev/null | cut -d= -f2-)
P=$(grep '^MQTT_CLOUD_PASS=' "$SEC" 2>/dev/null | cut -d= -f2-)
OUT=$(mosquitto_sub -h "$H" -p "$PORT" --cafile /etc/ssl/cert.pem -u "$U" -P "$P" \
      -t daijin/status -C 1 -W 5 2>/dev/null)
if [ -n "$OUT" ]; then
  echo "$OUT"
else
  echo "FAIL: 디바이스 상태 없음 (디바이스가 꺼져 있거나 아직 미보고)"; exit 1
fi
