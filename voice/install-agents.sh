#!/bin/bash
# 브레인·플릿 워처를 macOS LaunchAgent로 등록한다 (부팅 시 자동 시작, 죽으면 재시작).
# plist 안의 __REPO__/__HOME__ 자리를 이 저장소 위치로 채워 ~/Library/LaunchAgents 에 복사한 뒤 띄운다.
# 사용법: bash voice/install-agents.sh          (다시 실행하면 재등록)
#         bash voice/install-agents.sh remove   (해제)
set -e
VOICE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(dirname "$VOICE")"
DEST="$HOME/Library/LaunchAgents"; mkdir -p "$DEST"
for name in com.daijin.brain com.daijin.fleetwatch; do
  launchctl bootout "gui/$(id -u)/$name" 2>/dev/null || true
  if [ "$1" = "remove" ]; then rm -f "$DEST/$name.plist"; echo "해제: $name"; continue; fi
  sed "s|__REPO__|$REPO|g; s|__HOME__|$HOME|g" "$VOICE/$name.plist" > "$DEST/$name.plist"
  launchctl bootstrap "gui/$(id -u)" "$DEST/$name.plist"
  echo "등록: $name → $DEST/$name.plist"
done
[ "$1" = "remove" ] || launchctl list | grep daijin
