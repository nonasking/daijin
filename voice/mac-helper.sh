#!/bin/bash
# 맥을 "켜져 있으면 쓰는 도우미"로: 서버의 브레인이 사설망(Tailscale)으로 이 맥의 로컬 모델과 음성 인식을 쓰게 한다.
#   whisper.cpp 서버를 LaunchAgent 로 상시 띄우고(로그인하면 자동 시작), Tailscale serve 로 두 포트를 사설망에만 연다.
#   Ollama(11434) 와 whisper(18080) 는 이 맥의 127.0.0.1 에만 열려 있고, 공인 인터넷이나 집 와이파이에는 열지 않는다.
# 사용:  bash voice/mac-helper.sh install   whisper LaunchAgent 등록 + tailscale serve 설정 (Tailscale 앱에 로그인돼 있어야 serve 가 된다)
#        bash voice/mac-helper.sh remove
#        bash voice/mac-helper.sh status    이 맥의 사설망 주소와 서버 secrets 에 넣을 줄을 보여 준다
set -e
VOICE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL=com.daijin.whisper; PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
WS="$(command -v whisper-server || echo /opt/homebrew/bin/whisper-server)"
WM="$(ls "$VOICE"/models/ggml-large-v3-turbo*.bin "$HOME/dev/models"/ggml-large-v3-turbo*.bin 2>/dev/null | head -1)"
TS="$(command -v tailscale || echo /Applications/Tailscale.app/Contents/MacOS/Tailscale)"

case "${1:-status}" in
  install)
    [ -x "$WS" ] && [ -n "$WM" ] || { echo "whisper-server 나 모델이 없다"; exit 1; }
    cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$WS</string><string>-m</string><string>$WM</string><string>-l</string><string>ko</string>
    <string>--host</string><string>127.0.0.1</string><string>--port</string><string>18080</string></array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$VOICE/.offline/whisper-agent.log</string>
  <key>StandardErrorPath</key><string>$VOICE/.offline/whisper-agent.log</string>
</dict></plist>
PL
    mkdir -p "$VOICE/.offline"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && sleep 2 || true   # 바로 다시 올리면 "Input/output error"
    launchctl bootstrap "gui/$(id -u)" "$PLIST" && echo "whisper 서버 상시 실행 등록: $PLIST"
    if "$TS" status >/dev/null 2>&1; then
      "$TS" serve --bg --tcp 11434 tcp://127.0.0.1:11434 >/dev/null && echo "사설망에 Ollama(11434) 열음"
      "$TS" serve --bg --tcp 18080 tcp://127.0.0.1:18080 >/dev/null && echo "사설망에 whisper(18080) 열음"
    else
      echo "Tailscale 앱에 아직 로그인돼 있지 않다: 로그인한 뒤 이 명령을 다시 실행하면 사설망 설정까지 한다"
    fi ;;
  remove)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true; rm -f "$PLIST"
    "$TS" serve --tcp 11434 off 2>/dev/null || true; "$TS" serve --tcp 18080 off 2>/dev/null || true
    echo "해제됨" ;;
  status)
    launchctl list | grep -q "$LABEL" && echo "whisper 서버: 등록됨" || echo "whisper 서버: 없음"
    if ! "$TS" status >/dev/null 2>&1; then
      echo "Tailscale: 꺼져 있음 (메뉴 막대의 Tailscale 에서 Connect). 마지막 주소: $("$TS" ip -4 2>/dev/null || echo 없음)"
    elif IP=$("$TS" ip -4 2>/dev/null); then
      echo "이 맥의 사설망 주소: $IP (연결됨)"
      echo "서버 secrets 에 넣을 줄:"
      echo "  LOCAL_LLM_URL=http://$IP:11434/v1"
      echo "  WHISPER_URL=http://$IP:18080/inference"
      echo "  BRAIN_ENGINE=claude,local      (Claude 가 안 되면 맥의 모델로)"
    else
      echo "Tailscale: 로그인 안 됨"
    fi ;;
  *) echo "사용: mac-helper.sh install|remove|status"; exit 2 ;;
esac
