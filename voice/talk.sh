#!/bin/bash
# 맥-only 한국어 음성 비서 루프 (0단계, 보드 없이)
# 흐름: 마이크 녹음 → whisper(한국어 STT) → claude(두뇌) → say(한국어 TTS)
# 사용법:  bash ~/esp32-iot/voice/talk.sh    (종료: Ctrl+C)

DIR="$HOME/esp32-iot/voice"
MODEL="$DIR/models/ggml-large-v3-turbo-q5_0.bin"
WHISPER="/opt/homebrew/bin/whisper-cli"
WAV="/tmp/voice_in.wav"
VOICE="Yuna"
SYS="너는 음성으로 대화하는 친근한 비서야. 한국어로, 2~3문장 이내로 짧고 자연스럽게 답해. 이모지·마크다운·특수기호는 쓰지 마(음성으로 읽히니까). 집에 제어 가능한 LED가 있어 — 사용자가 불을 켜/꺼/색을 바꿔 달라고 하면 반드시 셸 명령 'bash ~/esp32-iot/voice/led.sh <색>' 을 실행해(색: red green blue yellow cyan magenta white off; '꺼줘'는 off, '켜줘'는 green). 실행 결과를 보고 한국어로 짧게 확인해. LED 요청이 아니면 그냥 대화해."

cd "$DIR" || exit 1
echo "🎙️  한국어 음성 비서 시작 (종료: Ctrl+C)"
echo "   STT=whisper(large-v3-turbo) · 두뇌=Claude · TTS=Yuna"

turn=0
while true; do
  echo
  read -r -p "▶︎ 엔터 누르고 말하세요 (시작)..."
  # 녹음 시작 → 다시 엔터로 종료
  sox -d -q -r 16000 -c 1 -b 16 "$WAV" >/dev/null 2>&1 &
  SOXPID=$!
  read -r -p "   🔴 녹음 중... (끝나면 엔터)"
  kill "$SOXPID" 2>/dev/null; wait "$SOXPID" 2>/dev/null

  # STT
  TXT=$("$WHISPER" -m "$MODEL" -l ko -nt -np -f "$WAV" 2>/dev/null | tr -d '\n' | sed 's/^ *//;s/ *$//')
  if [ -z "$TXT" ]; then echo "   (안 들렸어요 — 마이크 권한 확인)"; continue; fi
  echo "   🗣️  나: $TXT"

  # 두뇌 (Claude). 2턴부터 --continue 로 대화 맥락 유지
  if [ "$turn" -eq 0 ]; then
    RESP=$(claude -p "$TXT" --allowedTools "Bash" --append-system-prompt "$SYS" 2>/dev/null)
  else
    RESP=$(claude -p "$TXT" --continue --allowedTools "Bash" --append-system-prompt "$SYS" 2>/dev/null)
  fi
  turn=$((turn+1))

  # 이모지/마크다운 정리 (음성용)
  RESP=$(printf '%s' "$RESP" | python3 -c "import sys,re;print(re.sub(r'[\U0001F000-\U0001FAFF☀-➿←-⇿*#\`_]','',sys.stdin.read()).strip())")
  [ -z "$RESP" ] && RESP="죄송해요, 다시 한 번 말씀해 주세요."
  echo "   🤖 Claude: $RESP"

  # TTS 재생
  say -v "$VOICE" "$RESP"
done
