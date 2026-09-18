#!/bin/bash
# 홈 노드 플래시 헬퍼 — 노드 이름에 맞는 기능만 켜서 컴파일·업로드.
# 사용법: bash flash-node.sh <dev1|dev2> <포트> [home|hot1|hot2]
#   세 번째 인자: 그 망에만 붙게 빌드 (생략하면 아는 망 전부, 신호 센 곳 자동)
#   예:   bash flash-node.sh dev2 /dev/cu.usbmodem11401
# 포트 찾기: arduino-cli board list   (어느 보드인지 헷갈리면 esptool flash_id: HG DevKitC=16MB, Freenove 메인=8MB)
NODE="${1:?노드 이름 (dev1|dev2)}"; PORT="${2:?포트 (/dev/cu.usb...)}"; NET="${3:-}"
case "$NET" in home) NETF="-DWIFI_ONLY=1";; hot1) NETF="-DWIFI_ONLY=2";; hot2) NETF="-DWIFI_ONLY=3";; "") NETF="";; *) echo "망은 home|hot1|hot2"; exit 1;; esac
case "$NODE" in
  dev1) FLAGS="-DFEAT_SERVO=1 -DFEAT_BUZZER=1 -DFEAT_RELAY=1 -DFEAT_STEPPER=0 -DSERVO_REST=180" ;;   # 서보(대기 180도) + 부저 + 릴레이
  dev2) FLAGS="-DFEAT_SERVO=0 -DFEAT_BUZZER=1 -DFEAT_RELAY=0 -DFEAT_STEPPER=1 -DMOTOR_STEP_DEG=360" ;;   # 스텝모터(motor=한 바퀴) + 부저
  *)    FLAGS="" ;;                                                                 # 그 외: 펌웨어 기본값(전부 켬)
esac
cd "$(dirname "$0")" || exit 1
arduino-cli compile --fqbn esp32:esp32:esp32s3 \
  --build-property "compiler.cpp.extra_flags=-DNODE_NAME=\"$NODE\" $FLAGS $NETF" 15-home-node || exit 1
arduino-cli upload --fqbn esp32:esp32:esp32s3 -p "$PORT" 15-home-node
