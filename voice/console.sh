#!/bin/bash
# 관제 화면 — 촬영·디버깅용 라이브 토픽 모니터.
# 오디오 청크(바이너리 홍수)는 제외하고, 사람이 읽을 이벤트만 시간과 함께 흘려준다.
# 사용법: bash console.sh   (Ctrl-C로 종료)
SEC="$HOME/esp32-iot/secrets.local.txt"
H=$(grep '^MQTT_CLOUD_HOST=' "$SEC" | cut -d= -f2-)
PORT=$(grep '^MQTT_CLOUD_PORT=' "$SEC" | cut -d= -f2-)
U=$(grep '^MQTT_CLOUD_USER=' "$SEC" | cut -d= -f2-)
P=$(grep '^MQTT_CLOUD_PASS=' "$SEC" | cut -d= -f2-)
echo "═══ daijin 관제 화면 ═══ (오디오 스트림 제외 전 토픽)"
mosquitto_sub -h "$H" -p "$PORT" --cafile /etc/ssl/cert.pem -u "$U" -P "$P" -v \
  -t 'daijin/text/#' -t 'daijin/say' -t 'daijin/ask' -t 'daijin/status' \
  -t 'daijin/dev/+/cmd' -t 'daijin/dev/+/state' -t 'daijin/dev/+/status' -t 'daijin/dev/+/event' \
| while IFS= read -r line; do
    topic="${line%% *}"; payload="${line#* }"
    ts=$(date +%H:%M:%S)
    case "$topic" in
      daijin/text/in)      printf '\033[36m[%s] 🗣️  들림   \033[0m%s\n' "$ts" "$payload";;
      daijin/text/out)     printf '\033[32m[%s] 🤖 다이진  \033[0m%s\n' "$ts" "$payload";;
      daijin/say)          printf '\033[35m[%s] 📢 say    \033[0m%s\n' "$ts" "$payload";;
      daijin/ask)          printf '\033[35m[%s] 📨 ask    \033[0m%s\n' "$ts" "$payload";;
      daijin/status)       printf '\033[90m[%s] 💓 daijin \033[0m%s\n' "$ts" "$payload";;
      daijin/dev/*/cmd)    printf '\033[33m[%s] ⚡ 명령   \033[0m%s ← %s\n' "$ts" "$(echo "$topic" | cut -d/ -f3)" "$payload";;
      daijin/dev/*/state)  printf '\033[33m[%s] ✅ ACK    \033[0m%s: %s\n' "$ts" "$(echo "$topic" | cut -d/ -f3)" "$payload";;
      daijin/dev/*/status) printf '\033[90m[%s] 💓 %s   \033[0m%s\n' "$ts" "$(echo "$topic" | cut -d/ -f3)" "$payload";;
      daijin/dev/*/event)  printf '\033[31m[%s] 🚨 이벤트 \033[0m%s\n' "$ts" "$payload";;
      *)                   printf '[%s] %s %s\n' "$ts" "$topic" "$payload";;
    esac
  done
