#!/bin/bash
# 리눅스: 브레인·플릿 워처를 systemd 사용자 서비스로 등록한다 (부팅 시 자동 시작, 죽으면 재시작).
# 맥의 install-agents.sh 에 해당. 유닛 파일의 __REPO__/__HOME__/__PYTHON__ 자리를 채워 ~/.config/systemd/user 에 복사한다.
# 사용법: bash voice/install-services.sh          (다시 실행하면 재등록)
#         bash voice/install-services.sh remove   (해제)
#         PYTHON=/path/to/venv/bin/python3 bash voice/install-services.sh   (venv 파이썬을 쓸 때)
set -e
VOICE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(dirname "$VOICE")"
PY="${PYTHON:-$(command -v python3)}"
DEST="$HOME/.config/systemd/user"; mkdir -p "$DEST"
for name in daijin-brain daijin-fleetwatch; do
  if [ "$1" = "remove" ]; then
    systemctl --user disable --now "$name" 2>/dev/null || true
    rm -f "$DEST/$name.service"; echo "해제: $name"; continue
  fi
  sed "s|__REPO__|$REPO|g; s|__HOME__|$HOME|g; s|__PYTHON__|$PY|g" "$VOICE/systemd/$name.service" > "$DEST/$name.service"
  echo "등록: $name → $DEST/$name.service (python: $PY)"
done
systemctl --user daemon-reload
[ "$1" = "remove" ] && exit 0
systemctl --user enable --now daijin-brain daijin-fleetwatch
# linger: 로그인 세션이 없어도(SSH 끊김, 재부팅 후) 사용자 서비스가 계속 돈다. 배포판에 따라 sudo 가 필요할 수 있다.
loginctl enable-linger "$USER" 2>/dev/null || echo "참고: 'sudo loginctl enable-linger $USER' 를 한 번 실행해야 로그아웃 후에도 서비스가 유지된다"
systemctl --user --no-pager status daijin-brain daijin-fleetwatch | grep -E "●|Active:"
