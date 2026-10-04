#!/bin/bash
# 플릿 상태 조회 헬퍼 — Claude(daijin 두뇌)가 호출. 실제 일은 nodectl.py 가 한다.
# 모든 홈 노드의 retained status(하트비트/LWT)를 모아 온라인 여부와 함께 출력한다. 판정은 fleet_status.py:
#   online=false → OFFLINE(브로커 LWT가 남긴 것) · online=true인데 ts가 90초 초과 → STALE(하트비트 중단 의심) · 그 외 → ONLINE
# 사용법: bash fleet.sh
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$(bash "$D/py.sh")" "$D/nodectl.py" fleet
