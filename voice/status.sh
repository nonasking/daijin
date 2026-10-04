#!/bin/bash
# 디바이스 상태 조회 헬퍼 — Claude(daijin 두뇌)가 호출. 실제 일은 nodectl.py 가 한다.
# 디바이스가 daijin/status 에 retained로 발행하는 JSON(온도·WiFi·업타임)을 읽어온다.
# 사용법: bash status.sh
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$(bash "$D/py.sh")" "$D/nodectl.py" status
