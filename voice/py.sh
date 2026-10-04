#!/bin/bash
# 헬퍼(dev.sh 등)가 nodectl.py 를 돌릴 파이썬을 한 줄로 알려준다: paho-mqtt 가 깔려 있는 것.
# 브레인이 띄운 셸이면 DAIJIN_PYTHON(브레인 자신의 파이썬)이 이미 정해져 있다. 손으로 부를 땐 후보를 차례로 본다.
if [ -n "$DAIJIN_PYTHON" ]; then echo "$DAIJIN_PYTHON"; exit 0; fi
for c in "$HOME/venv/bin/python3" python3 /opt/homebrew/bin/python3; do
  if "$c" -c 'import paho.mqtt.client' 2>/dev/null; then echo "$c"; exit 0; fi
done
echo python3    # 못 찾으면 그대로 실행해서 ImportError 가 원인을 말하게 둔다
