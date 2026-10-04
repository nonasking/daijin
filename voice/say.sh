#!/bin/bash
# 능동 발화 헬퍼 — 다이진 스피커로 말하게 한다. 실제 일은 nodectl.py 가 한다.
# 사용법: bash say.sh "안녕"                  → 그대로 말함 (daijin/say)
#         bash say.sh -a "dev2가 오프라인 됐어" → 에이전트가 스스로 표현해 말함 (daijin/ask)
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$(bash "$D/py.sh")" "$D/nodectl.py" say "$@"
