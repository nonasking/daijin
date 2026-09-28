#!/bin/bash
# 플릿 상태 조회 헬퍼 — Claude(daijin 두뇌)가 호출.
# 모든 홈 노드의 retained status(하트비트/LWT)를 모아 온라인 여부와 함께 출력한다.
# 판정(fleet_status.py): online=false → OFFLINE(브로커 LWT가 남긴 것)
#                        online=true인데 ts가 90초 초과 → STALE(하트비트 중단 의심)
#                        그 외 → ONLINE
# 사용법: bash fleet.sh
source "$(dirname "${BASH_SOURCE[0]}")/broker.sh"   # AUTH/H/PORT/SEC — 로컬 브리지 우선, 없으면 클라우드 (맥·리눅스 공통)
RAW=$(mosquitto_sub "${AUTH[@]}" \
      -t 'daijin/dev/+/status' -v -W $([ "$H" = localhost ] && echo 1 || echo 2) 2>/dev/null)   # retained는 즉시 오므로 로컬은 1초면 충분
if [ -z "$RAW" ]; then
  echo "노드 없음 (retained status가 하나도 없음 — 아직 아무 노드도 접속한 적 없음)"
  exit 1
fi
echo "$RAW" | python3 "$(dirname "${BASH_SOURCE[0]}")/fleet_status.py"
