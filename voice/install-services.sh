#!/bin/bash
# 리눅스: 브레인·플릿 워처·initiative·밤 반성 타이머를 systemd 사용자 서비스로 등록한다 (부팅 시 자동 시작, 죽으면 재시작).
# 맥의 install-agents.sh 에 해당. 유닛 파일의 __REPO__/__HOME__/__PYTHON__ 자리를 채워 ~/.config/systemd/user 에 복사한다.
# 사용법: bash voice/install-services.sh          (다시 실행하면 재등록; 이미 돌고 있는 서비스는 재시작하지 않는다 → systemctl --user restart)
#         bash voice/install-services.sh remove   (해제)
#         PYTHON=/path/to/venv/bin/python3 bash voice/install-services.sh   (venv 파이썬을 쓸 때)
#         WITH_WHISPER=1 bash voice/install-services.sh                      (로컬 whisper.cpp 서버도 함께; 홈서버용)
set -e
VOICE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(dirname "$VOICE")"
PY="${PYTHON:-$(command -v python3)}"
DEST="$HOME/.config/systemd/user"; mkdir -p "$DEST"
UNITS="daijin-brain.service daijin-fleetwatch.service daijin-initiative.service daijin-reflect.service daijin-reflect.timer"
if [ "${WITH_WHISPER:-0}" = 1 ]; then
  WS="$(command -v whisper-server || true)"; WM="${WHISPER_MODEL:-$VOICE/models/ggml-large-v3-turbo-q5_0.bin}"
  [ -n "$WS" ] && [ -f "$WM" ] || { echo "whisper-server 실행 파일이나 모델($WM)이 없다"; exit 1; }
  UNITS="$UNITS whisper-server.service"
fi
for unit in $UNITS; do
  if [ "$1" = "remove" ]; then
    systemctl --user disable --now "$unit" 2>/dev/null || true
    rm -f "$DEST/$unit"; echo "해제: $unit"; continue
  fi
  sed "s|__REPO__|$REPO|g; s|__HOME__|$HOME|g; s|__PYTHON__|$PY|g; s|__WHISPER_SERVER__|${WS:-}|g; s|__WHISPER_MODEL__|${WM:-}|g" \
      "$VOICE/systemd/$unit" > "$DEST/$unit"
  echo "등록: $unit → $DEST/$unit"
done
systemctl --user daemon-reload
[ "$1" = "remove" ] && exit 0
# reflect.service 는 타이머가 띄우므로 enable 하지 않는다 (지금 바로 돌려 보려면 systemctl --user start daijin-reflect)
ENABLE="daijin-brain daijin-fleetwatch daijin-initiative daijin-reflect.timer"
[ "${WITH_WHISPER:-0}" = 1 ] && ENABLE="$ENABLE whisper-server"
systemctl --user enable --now $ENABLE
# linger: 로그인 세션이 없어도(SSH 끊김, 재부팅 후) 사용자 서비스가 계속 돈다. 배포판에 따라 sudo 가 필요할 수 있다.
loginctl enable-linger "$USER" 2>/dev/null || echo "참고: 'sudo loginctl enable-linger $USER' 를 한 번 실행해야 로그아웃 후에도 서비스가 유지된다"
systemctl --user --no-pager status $ENABLE | grep -E "●|Active:|Trigger:"
echo "로그: journalctl --user -u daijin-brain -f   (-u daijin-fleetwatch, -u daijin-initiative, -u daijin-reflect)"
echo "인격 제안 확인: $PY $VOICE/persona.py show"
