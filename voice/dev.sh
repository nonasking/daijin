#!/bin/bash
# 홈 노드 명령 헬퍼: Claude(daijin 두뇌)가 호출.
# 사용법: bash dev.sh <노드|all> <명령>
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
#
# ACK 방식: state 토픽은 retained라 그냥 읽으면 "직전" 명령의 ACK가 먼저 튀어나온다(pong 오답의 원인).
# 그래서 구독을 먼저 열고(-R: retained 무시) 그 다음 publish → 이번 명령의 ACK만 잡는다.
NODE="${1:?노드 이름 필요 (1, 1번, dev1, 2, 2번, dev2, all)}"
case "$NODE" in 1|1번|red|레드) NODE=dev1;; 2|2번|blue|블루) NODE=dev2;; esac   # 음성 호칭은 레드·블루, 내부 ID는 1번·2번
CMD="${2:?명령 필요 (예: led:red, poke:120, relay:on)}"
SEC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/secrets.local.txt"   # 저장소 루트의 secrets (클론 위치 무관)
# 로컬 mosquitto(1883, 평문)로 붙는다 — HiveMQ 브리지가 클라우드와 동기화 (mosquitto.conf 참고).
# 클라우드 직결(TLS 왕복 1.3초)보다 명령당 3초 이상 빠르다. 로컬 브로커가 죽어 있으면 클라우드로 폴백.
H=localhost; PORT=1883
U=$(grep '^MQTT_USER=' "$SEC" 2>/dev/null | cut -d= -f2-)
P=$(grep '^MQTT_PASS=' "$SEC" 2>/dev/null | cut -d= -f2-)
CAFILE=()
if ! nc -z localhost 1883 2>/dev/null; then
  H=$(grep '^MQTT_CLOUD_HOST=' "$SEC" 2>/dev/null | cut -d= -f2-)
  PORT=$(grep '^MQTT_CLOUD_PORT=' "$SEC" 2>/dev/null | cut -d= -f2-)
  U=$(grep '^MQTT_CLOUD_USER=' "$SEC" 2>/dev/null | cut -d= -f2-)
  P=$(grep '^MQTT_CLOUD_PASS=' "$SEC" 2>/dev/null | cut -d= -f2-)
  CAFILE=(--cafile /etc/ssl/cert.pem)
fi
AUTH=(-h "$H" -p "$PORT" "${CAFILE[@]}" -u "$U" -P "$P")

if [ "$NODE" = "all" ]; then
  if mosquitto_pub "${AUTH[@]}" -t "daijin/dev/all/cmd" -m "$CMD"; then
    echo "OK: 전 노드 <- $CMD"; exit 0
  else
    echo "FAIL: 브로커 전송 실패"; exit 1
  fi
fi

# 오프라인 노드는 8초 기다리지 말고 retained status로 즉시 판정 (로컬 브로커면 0.01초)
ST=$(mosquitto_sub "${AUTH[@]}" -t "daijin/dev/$NODE/status" -C 1 -W 1 2>/dev/null)
case "$ST" in
  *'"online":false'*) echo "FAIL: $NODE 오프라인 (전원 또는 네트워크 끊김) — 명령 안 보냄"; exit 1;;
  "") echo "참고: $NODE 상태 기록 없음 (한 번도 접속한 적 없는 이름?) — 일단 보내봄";;
esac
ACKFILE=$(mktemp)
# 동작(step:360은 5초, poke/alarm 최대 3초) + EU 브로커 왕복까지 넉넉히 14초
mosquitto_sub "${AUTH[@]}" -t "daijin/dev/$NODE/state" -R -C 1 -W 14 > "$ACKFILE" 2>/dev/null &
SUBPID=$!
if [ "$H" = localhost ]; then sleep 0.2; else sleep 1.5; fi   # 구독 성립 대기 (로컬은 즉시)
if ! mosquitto_pub "${AUTH[@]}" -t "daijin/dev/$NODE/cmd" -m "$CMD"; then
  kill $SUBPID 2>/dev/null; rm -f "$ACKFILE"
  echo "FAIL: 브로커 전송 실패"; exit 1
fi
wait $SUBPID
ACK=$(cat "$ACKFILE"); rm -f "$ACKFILE"
echo "OK: $NODE <- $CMD (ack: ${ACK:-없음: 노드가 오프라인이거나 응답 지연})"
case "$ACK" in
  servo:*:nochange)
    echo "참고: 서보가 이미 그 각도라 움직임이 안 보였을 거야. 눈에 보이게 돌리려면 'motor' (갔다가 돌아옴)를 써."
    exit 0;;
  err:unknown:*)
    # 모르는 명령이면 그 자리에서 기능 목록을 읽어 같이 보여준다 (브레인이 따로 조회할 필요 없게)
    CAPS=$(mosquitto_sub "${AUTH[@]}" -t "daijin/dev/$NODE/state" -R -C 1 -W 6 2>/dev/null & sleep 0.3; mosquitto_pub "${AUTH[@]}" -t "daijin/dev/$NODE/cmd" -m caps; wait)
    echo "이 노드($NODE)에 없는 명령. 쓸 수 있는 기능: ${CAPS#caps:}  (모터를 돌리려면 노드 종류와 상관없이 motor 를 써)"
    exit 1;;
  err:*|"") exit 1;;
esac
