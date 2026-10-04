#!/bin/bash
# 홈 노드 명령 헬퍼: Claude(daijin 두뇌)가 호출. 실제 일은 nodectl.py 가 한다 (브로커 접속 하나로 구독·발행·ack 확인).
# 사용법: bash dev.sh <노드|all> <명령>      노드는 레드/블루, red/blue, 1/2, 1번/2번, dev1/dev2 다 받는다 (voice/nodes.json)
#   예:   bash dev.sh dev1 led:red
#         bash dev.sh dev2 servo:90        # 절대 각도
#         bash dev.sh dev2 poke:120:600    # 120도로 갔다가 0.6초 뒤 원위치 (스위치·풍선)
#         bash dev.sh dev2 step:180        # 스텝모터 반 바퀴 (음수=반대), 180도≈3초
#         bash dev.sh dev1 motor           # 노드가 뭐든 "모터 돌려": 서보면 poke, 스텝이면 step (motor:각도 도 됨)
#         bash dev.sh dev1 relay:on        # relay:off / relay:pulse:800 / fan:on
#         bash dev.sh dev1 buzz:alarm      # buzz:ok / buzz:fail / buzz:300 / buzz:1500:200
#         bash dev.sh dev1 caps            # 노드가 켜둔 기능 목록
#         bash dev.sh all  led:off
# 명령 형식은 15-home-node.ino 상단 명령표 참고.
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${1:?노드 이름 필요 (레드, 블루, 1, 2, dev1, dev2, all)}" "${2:?명령 필요 (예: led:red, poke:120, relay:on)}"
exec "$(bash "$D/py.sh")" "$D/nodectl.py" cmd "$1" "$2"
