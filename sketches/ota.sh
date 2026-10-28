#!/bin/bash
# 보드를 USB 없이 갱신한다: 빌드 → 서버(nginx, HTTPS)에 올림 → MQTT 로 ota 명령 → 새 fw 가 status 에 보일 때까지 기다림.
# 사용: bash sketches/ota.sh <dev1|dev2|daijin> [home|hot1|hot2]      (노드의 망 고정 인자는 flash-node.sh 와 같다)
#       bash sketches/ota.sh dev2 --build-only                           빌드와 해시만 (서버·보드는 건드리지 않는다)
# 전제: 보드에 OTA 가 든 펌웨어(ota.h)가 한 번은 USB 로 올라가 있어야 한다. 그 뒤로는 이 스크립트만.
#       맥 secrets.local.txt 에 FW_TOKEN(voice/ota/install-server.sh 가 준 값), SELF_SYNC_FROM(user@host), MQTT_CLOUD_HOST(=도메인).
# 보드 쪽: ota.h 가 HTTPS 로 받아 SHA-256 을 맞춰 보고 다른 슬롯에 써서 재부팅한다. 해시가 다르면 버리고 예전 펌웨어 그대로 돈다.
set -e -o pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(dirname "$HERE")"; VOICE="$REPO/voice"
sec() { grep "^$1=" "$REPO/secrets.local.txt" 2>/dev/null | cut -d= -f2-; }
TARGET="${1:?대상: dev1|dev2|daijin}"; NET="${2:-}"; BUILD_ONLY=""; [ "$NET" = "--build-only" ] && { BUILD_ONLY=1; NET=""; }
case "$NET" in home) NETF="-DWIFI_ONLY=1";; hot1) NETF="-DWIFI_ONLY=2";; hot2) NETF="-DWIFI_ONLY=3";; "") NETF="";; *) echo "망은 home|hot1|hot2"; exit 1;; esac

FWV="$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo nogit)-$(date +%y%m%d%H%M)"
OUT="$(mktemp -d)"; trap 'rm -rf "$OUT"' EXIT
case "$TARGET" in
  dev1|dev2)
    SKETCH=15-home-node; OPTS=()
    FLAGS="-DNODE_NAME=\"$TARGET\" -DFW_VERSION=\"$FWV\" $(python3 "$VOICE/nodes.py" flash "$TARGET") $NETF"
    T_CMD="daijin/dev/$TARGET/cmd"; T_STATUS="daijin/dev/$TARGET/status"; T_RESULT="daijin/dev/$TARGET/state" ;;
  daijin)
    SKETCH=14-daijin; OPTS=(--board-options CDCOnBoot=cdc)
    FLAGS="-DFW_VERSION=\"$FWV\""
    T_CMD="daijin/cmd"; T_STATUS="daijin/status"; T_RESULT="daijin/ota" ;;
  *) echo "대상은 dev1|dev2|daijin"; exit 1 ;;
esac

echo "빌드 $SKETCH ($TARGET, fw $FWV)"
arduino-cli compile --fqbn esp32:esp32:esp32s3 "${OPTS[@]}" --build-property "compiler.cpp.extra_flags=$FLAGS" \
  --output-dir "$OUT" "$HERE/$SKETCH" 2>&1 | grep -E "스케치는|error" || true
BIN="$OUT/$SKETCH.ino.bin"; [ -s "$BIN" ] || { echo "빌드 산출물이 없다: $BIN"; exit 1; }
SHA="$(shasum -a 256 "$BIN" | cut -d' ' -f1)"; NAME="$TARGET-$FWV.bin"
echo "  $(du -h "$BIN" | cut -f1)  sha256 $SHA"
[ -n "$BUILD_ONLY" ] && { cp "$BIN" "./$NAME"; echo "저장: ./$NAME"; exit 0; }

TOKEN="$(sec FW_TOKEN)"; FROM="$(sec SELF_SYNC_FROM)"; DOMAIN="$(sec MQTT_CLOUD_HOST)"
[ -n "$TOKEN" ] && [ -n "$FROM" ] && [ -n "$DOMAIN" ] || { echo "secrets 에 FW_TOKEN, SELF_SYNC_FROM, MQTT_CLOUD_HOST 가 필요하다"; exit 1; }
case "$DOMAIN" in 127.0.0.1|localhost) echo "MQTT_CLOUD_HOST 가 $DOMAIN: 보드가 받을 수 있는 도메인이어야 한다"; exit 1;; esac
DIR="/var/www/daijin/fw/$TOKEN"
echo "올림 → $FROM:$DIR/$NAME"
scp -q "$BIN" "$FROM:$DIR/$NAME"
ssh -o BatchMode=yes "$FROM" "cd '$DIR' && ls -t '$TARGET'-*.bin | tail -n +3 | xargs -r rm -f"     # 대상마다 최근 2개만 둔다
URL="https://$DOMAIN/fw/$TOKEN/$NAME"
curl -sfI --max-time 10 "$URL" >/dev/null || { echo "서버에서 못 받는다: $URL"; exit 1; }

echo "명령 → $T_CMD  (기다림: $T_STATUS 의 fw == $FWV, 최대 150초)"
VOICE_DIR="$VOICE" "$(bash "$VOICE/py.sh")" - "$T_CMD" "$T_STATUS" "$T_RESULT" "ota:$URL:$SHA" "$FWV" <<'PY'
import json, sys, time
sys.path.insert(0, __import__("os").environ.get("VOICE_DIR", "."))
import daijin_common as dc
t_cmd, t_status, t_result, cmd, want = sys.argv[1:6]
seen = {"fw": None, "online": None, "result": [], "done": False}
def on_msg(c, u, m):
    p = m.payload.decode("utf-8", "replace")
    if m.topic == t_status:
        try: d = json.loads(p)
        except ValueError: return
        seen["online"], seen["fw"] = d.get("online"), d.get("fw")
        if d.get("online") and d.get("fw") == want: seen["done"] = True
    elif m.topic == t_result and p.startswith("ota:"):
        seen["result"].append(p); print(f"  보드: {p}", flush=True)
c = dc.cloud_client(""); c.on_message = on_msg
c.on_connect = lambda cl, u, f, r, pr=None: cl.subscribe([(t_status, 1), (t_result, 1)])
dc.connect_with_retry(c); c.loop_start(); time.sleep(2)
if seen["online"] is False or seen["fw"] is None and seen["online"] is None:
    print(f"  보드가 오프라인이거나 status 가 없다 (online={seen['online']}, fw={seen['fw']})"); sys.exit(2)
print(f"  지금 fw {seen['fw']} → 보내는 중", flush=True)
t0 = time.time(); tries = 0
while tries < 2 and not seen["done"]:                 # 내려받기가 중간에 끊기면(ota:err:short, 와이파이 흔들림) 한 번 더 보낸다
    tries += 1; seen["result"].clear()
    c.publish(t_cmd, cmd, qos=1); t1 = time.time()
    while time.time() - t1 < 150 and not seen["done"]:
        err = next((r for r in seen["result"] if r.startswith("ota:err")), None)
        if err:
            if err.startswith("ota:err:short") and tries < 2: print("  내려받기가 끊겼다 → 한 번 더", flush=True); time.sleep(3); break
            print("  실패: 보드가 거절했다 (예전 펌웨어 그대로)"); sys.exit(1)
        time.sleep(0.5)
c.loop_stop()
if seen["done"]: print(f"  완료: fw {want} 로 재부팅해 올라왔다 ({time.time()-t0:.0f}초)")
else: print(f"  시간 초과: 아직 fw {seen['fw']} (online={seen['online']}). 보드 시리얼이나 {t_result} 를 볼 것"); sys.exit(1)
PY
