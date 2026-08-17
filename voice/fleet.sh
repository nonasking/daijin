#!/bin/bash
# 플릿 상태 조회 헬퍼 — Claude(daijin 두뇌)가 호출.
# 모든 홈 노드의 retained status(하트비트/LWT)를 모아 온라인 여부와 함께 출력한다.
# 판정(fleet_status.py): online=false → OFFLINE(브로커 LWT가 남긴 것)
#                        online=true인데 ts가 90초 초과 → STALE(하트비트 중단 의심)
#                        그 외 → ONLINE
# 사용법: bash fleet.sh
SEC="$HOME/esp32-iot/secrets.local.txt"
H=$(grep '^MQTT_CLOUD_HOST=' "$SEC" 2>/dev/null | cut -d= -f2-)
PORT=$(grep '^MQTT_CLOUD_PORT=' "$SEC" 2>/dev/null | cut -d= -f2-)
U=$(grep '^MQTT_CLOUD_USER=' "$SEC" 2>/dev/null | cut -d= -f2-)
P=$(grep '^MQTT_CLOUD_PASS=' "$SEC" 2>/dev/null | cut -d= -f2-)
RAW=$(mosquitto_sub -h "$H" -p "$PORT" --cafile /etc/ssl/cert.pem -u "$U" -P "$P" \
      -t 'daijin/dev/+/status' -v -W 2 2>/dev/null)
if [ -z "$RAW" ]; then
  echo "노드 없음 (retained status가 하나도 없음 — 아직 아무 노드도 접속한 적 없음)"
  exit 1
fi
echo "$RAW" | python3 "$HOME/esp32-iot/voice/fleet_status.py"
