#!/bin/bash
# 홈 노드 명령 헬퍼 — Claude(daijin 두뇌)가 호출.
# 사용법: bash dev.sh <노드|all> <명령>
#   예:   bash dev.sh dev1 led:red
#         bash dev.sh dev2 servo:90
#         bash dev.sh dev1 relay:on
#         bash dev.sh all  led:off
# 명령 형식은 15-home-node.ino의 runCmd 참고 (led/servo/relay/fan/ping).
NODE="${1:?노드 이름 필요 (dev1, dev2, all ...)}"
CMD="${2:?명령 필요 (예: led:red, servo:90, relay:on)}"
SEC="$HOME/esp32-iot/secrets.local.txt"
H=$(grep '^MQTT_CLOUD_HOST=' "$SEC" 2>/dev/null | cut -d= -f2-)
PORT=$(grep '^MQTT_CLOUD_PORT=' "$SEC" 2>/dev/null | cut -d= -f2-)
U=$(grep '^MQTT_CLOUD_USER=' "$SEC" 2>/dev/null | cut -d= -f2-)
P=$(grep '^MQTT_CLOUD_PASS=' "$SEC" 2>/dev/null | cut -d= -f2-)
if mosquitto_pub -h "$H" -p "$PORT" --cafile /etc/ssl/cert.pem -u "$U" -P "$P" \
     -t "daijin/dev/$NODE/cmd" -m "$CMD"; then
  # ACK 확인: 노드가 state에 retained로 남긴 실행 결과를 3초 안에 읽는다
  if [ "$NODE" != "all" ]; then
    ACK=$(mosquitto_sub -h "$H" -p "$PORT" --cafile /etc/ssl/cert.pem -u "$U" -P "$P" \
          -t "daijin/dev/$NODE/state" -C 1 -W 3 2>/dev/null)
    echo "OK: $NODE <- $CMD (ack: ${ACK:-없음})"
    case "$ACK" in err:*) exit 1;; esac
  else
    echo "OK: 전 노드 <- $CMD"
  fi
else
  echo "FAIL: 브로커 전송 실패"; exit 1
fi
