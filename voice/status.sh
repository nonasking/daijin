#!/bin/bash
# 디바이스 상태 조회 헬퍼 — Claude(daijin 두뇌)가 호출.
# 디바이스가 daijin/status 에 retained로 발행하는 JSON(온도·WiFi·업타임)을 읽어온다.
# 사용법: bash status.sh
source "$(dirname "${BASH_SOURCE[0]}")/broker.sh"   # AUTH/H/PORT/SEC — 로컬 브리지 우선, 없으면 클라우드 (맥·리눅스 공통)
OUT=$(mosquitto_sub "${AUTH[@]}" \
      -t daijin/status -C 1 -W 5 2>/dev/null)
if [ -n "$OUT" ]; then
  echo "$OUT"
else
  echo "FAIL: 디바이스 상태 없음 (디바이스가 꺼져 있거나 아직 미보고)"; exit 1
fi
