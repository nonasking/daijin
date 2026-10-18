#!/bin/bash
# 비상 모드 — 외부 서비스 없이 이 기계(맥)에서 다이진을 통째로 돌린다.
#   생각: 로컬 모델 (Ollama 등, LOCAL_LLM_URL)   말 알아듣기: whisper.cpp 서버   목소리: Supertonic   우체국: 이 기계의 mosquitto
# 기기·노드는 펌웨어에 집 우체국(MQTT_LOCAL_HOST = 이 기계의 IP)을 넣어 두면, 인터넷이 끊겼을 때 스스로 여기로 온다.
# secrets.local.txt 는 건드리지 않는다: 이 스크립트가 띄우는 프로세스만 DAIJIN_* 환경 변수로 설정을 바꾼다.
#
# 사용:  bash voice/offline-mode.sh start    whisper 서버 + 브레인 + 플릿 워처 + initiative 를 띄운다
#        bash voice/offline-mode.sh stop
#        bash voice/offline-mode.sh status
#        bash voice/offline-mode.sh sync     (인터넷이 될 때) 서버의 자아 층 DB 를 이 기계로 복사해 둔다
#                                           서버: secrets 의 SELF_SYNC_FROM=user@host (예: daijin@1.2.3.4)
#                                           아직 서버로 올리지 않은 비상 모드 기록이 있으면 덮어쓰지 않고 멈춘다 (먼저 push)
#        bash voice/offline-mode.sh push     (인터넷이 돌아오면) 지난 sync 뒤에 이 기계에서 쌓인 대화·장부·기억을 서버 DB 에 합친다
#                                           (selfdb_merge.py: 파일을 덮어쓰지 않고 행을 합친다, 두 번 돌려도 안전)
# 주의: 인터넷이 될 때 켜 두어도 서버 브레인과 부딪히지 않는다 (음성 토픽은 집↔클라우드 사이를 건너지 않는다).
#       다만 기기가 클라우드에 붙어 있는 동안은 이쪽 브레인에 말이 오지 않는다.
set -e
VOICE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(dirname "$VOICE")"
RUN="$VOICE/.offline"; mkdir -p "$RUN"
PY="${PYTHON:-/opt/homebrew/bin/python3}"
WS="$(command -v whisper-server || echo /opt/homebrew/bin/whisper-server)"
MODEL_DIR="${WHISPER_MODEL_DIR:-$VOICE/models}"
WM="$(ls "$MODEL_DIR"/ggml-large-v3-turbo*.bin "$HOME/dev/models"/ggml-large-v3-turbo*.bin 2>/dev/null | head -1)"
sec() { grep "^$1=" "$REPO/secrets.local.txt" 2>/dev/null | cut -d= -f2-; }

export DAIJIN_BROKER=local DAIJIN_BRAIN_ENGINE=local DAIJIN_REFLECT_ENGINE=local \
       DAIJIN_STT_ENGINE=whisper_server DAIJIN_WHISPER_URL=http://127.0.0.1:18080/inference \
       DAIJIN_TTS_ENGINE=supertonic DAIJIN_FAST_COMMANDS=1 DAIJIN_SELF_DB="$VOICE/daijin.db"

up()   { [ -f "$RUN/$1.pid" ] && kill -0 "$(cat "$RUN/$1.pid")" 2>/dev/null; }
launch() {  # 이름, 명령...
  local name="$1"; shift
  if up "$name"; then echo "  $name 이미 돎 (pid $(cat "$RUN/$name.pid"))"; return; fi
  nohup "$@" >> "$RUN/$name.log" 2>&1 &
  echo $! > "$RUN/$name.pid"; echo "  $name 시작 (pid $!, 로그 $RUN/$name.log)"
}

case "${1:-status}" in
  start)
    echo "== 확인"
    nc -z 127.0.0.1 1883 && echo "  우체국(mosquitto) 있음" || { echo "  우체국 없음: brew services start mosquitto"; exit 1; }
    U="${DAIJIN_LOCAL_LLM_URL:-$(sec LOCAL_LLM_URL)}"; U="${U:-http://127.0.0.1:11434/v1}"
    curl -s -m 3 "$U/models" >/dev/null && echo "  로컬 모델 있음 ($U)" || { echo "  로컬 모델에 닿지 않음 ($U): brew services start ollama"; exit 1; }
    "$PY" -c "import paho.mqtt.client, supertonic, numpy" 2>/dev/null && echo "  파이썬 패키지 있음 ($PY)" || { echo "  $PY 에 paho-mqtt supertonic numpy 필요"; exit 1; }
    [ -x "$WS" ] && [ -n "$WM" ] || { echo "  whisper-server 나 모델이 없다 (brew install whisper-cpp, 모델은 voice/models/)"; exit 1; }
    echo "== 시작"
    if nc -z 127.0.0.1 18080 2>/dev/null; then echo "  whisper 서버 이미 떠 있음 (mac-helper 의 상시 실행)"
    else launch whisper "$WS" -m "$WM" -l ko --host 127.0.0.1 --port 18080; fi
    for i in $(seq 1 30); do nc -z 127.0.0.1 18080 && break; sleep 1; done
    cd "$VOICE"
    launch brain "$PY" -u daijin_mqtt.py
    launch fleetwatch "$PY" -u fleet_watch.py
    launch initiative "$PY" -u initiative.py
    echo "비상 모드 켜짐. 기기가 집 우체국에 붙으면 이 기계가 대답한다. 끄기: bash voice/offline-mode.sh stop"
    ;;
  stop)
    for n in initiative fleetwatch brain whisper; do
      if up "$n"; then kill "$(cat "$RUN/$n.pid")" && echo "  $n 멈춤"; fi
      rm -f "$RUN/$n.pid"
    done ;;
  status)
    for n in whisper brain fleetwatch initiative; do
      if up "$n"; then echo "  $n: 돎 (pid $(cat "$RUN/$n.pid"))"; else echo "  $n: 꺼짐"; fi
    done ;;
  sync)
    FROM="$(sec SELF_SYNC_FROM)"; [ -n "$FROM" ] || { echo "secrets 에 SELF_SYNC_FROM=user@host 가 필요하다"; exit 1; }
    if [ -f "$VOICE/daijin.db" ] && [ "${2:-}" != "--force" ]; then
      n="$("$PY" "$VOICE/selfdb_merge.py" unpushed "$VOICE/daijin.db")"
      [ "$n" = "0" ] || { echo "이 기계에 아직 서버로 올리지 않은 기록이 ${n}건 있다. 먼저: bash voice/offline-mode.sh push (버리려면 sync --force)"; exit 1; }
    fi
    ssh -o BatchMode=yes -o ConnectTimeout=10 "$FROM" 'cd ~/esp32-iot/voice && sqlite3 daijin.db ".backup /tmp/daijin-sync.db" 2>/dev/null || ~/venv/bin/python3 -c "import sqlite3; s=sqlite3.connect(\"daijin.db\"); d=sqlite3.connect(\"/tmp/daijin-sync.db\"); s.backup(d)"'
    scp -q "$FROM:/tmp/daijin-sync.db" "$VOICE/daijin.db.new" && ssh -o BatchMode=yes "$FROM" 'rm -f /tmp/daijin-sync.db'
    mv "$VOICE/daijin.db.new" "$VOICE/daijin.db"; rm -f "$VOICE/daijin.db-wal" "$VOICE/daijin.db-shm"
    "$PY" "$VOICE/selfdb_merge.py" mark "$VOICE/daijin.db" pulled
    echo "자아 층 DB 를 $FROM 에서 가져왔다 ($(du -h "$VOICE/daijin.db" | cut -f1))" ;;
  push)
    FROM="$(sec SELF_SYNC_FROM)"; [ -n "$FROM" ] || { echo "secrets 에 SELF_SYNC_FROM=user@host 가 필요하다"; exit 1; }
    [ -f "$VOICE/daijin.db" ] || { echo "이 기계에 자아 층 DB 가 없다"; exit 1; }
    SINCE="$("$PY" "$VOICE/selfdb_merge.py" since "$VOICE/daijin.db")"
    [ "$SINCE" != "0" ] || { echo "이 DB 는 sync 로 가져온 표시가 없다: 어디서부터 올릴지 몰라 멈춘다"; exit 1; }
    NOW="$(date +%s)"; SNAP="$RUN/push-snapshot.db"; rm -f "$SNAP"
    "$PY" -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d)" "$VOICE/daijin.db" "$SNAP"
    scp -q "$SNAP" "$FROM:/tmp/daijin-push.db"
    ssh -o BatchMode=yes "$FROM" "cd ~/esp32-iot/voice && ~/venv/bin/python3 selfdb_merge.py /tmp/daijin-push.db daijin.db $SINCE; rc=\$?; rm -f /tmp/daijin-push.db; exit \$rc"
    "$PY" "$VOICE/selfdb_merge.py" mark "$VOICE/daijin.db" pushed "$NOW"; rm -f "$SNAP"
    echo "$(date -r "$SINCE" '+%m-%d %H:%M') 이후의 기록을 $FROM 의 자아 층에 합쳤다" ;;
  *) echo "사용: offline-mode.sh start|stop|status|sync|push"; exit 2 ;;
esac
