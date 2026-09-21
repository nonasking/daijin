#!/bin/bash
# 플릿 상태 조회 헬퍼 — Claude(daijin 두뇌)가 호출.
# 모든 홈 노드의 retained status(하트비트/LWT)를 모아 온라인 여부와 함께 출력한다.
# 판정(fleet_status.py): online=false → OFFLINE(브로커 LWT가 남긴 것)
#                        online=true인데 ts가 90초 초과 → STALE(하트비트 중단 의심)
#                        그 외 → ONLINE
# 사용법: bash fleet.sh
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
RAW=$(mosquitto_sub -h "$H" -p "$PORT" "${CAFILE[@]}" -u "$U" -P "$P" \
      -t 'daijin/dev/+/status' -v -W $([ "$H" = localhost ] && echo 1 || echo 2) 2>/dev/null)   # retained는 즉시 오므로 로컬은 1초면 충분
if [ -z "$RAW" ]; then
  echo "노드 없음 (retained status가 하나도 없음 — 아직 아무 노드도 접속한 적 없음)"
  exit 1
fi
echo "$RAW" | python3 "$(dirname "${BASH_SOURCE[0]}")/fleet_status.py"
