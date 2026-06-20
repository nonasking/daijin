#!/usr/bin/env python3
# 가짜 디바이스 — ESP32 흉내. 샘플 WAV를 daijin/audio/in 에 발행하고,
# daijin/audio/out 에서 답 음성을 받아 재생(afplay). ESP32 없이 클라우드 왕복 테스트용.
# 사용:  python3 test_device.py <질문.wav>

import paho.mqtt.client as mqtt
import sys, os, ssl, subprocess, time

HOME = os.path.expanduser("~")
SEC  = f"{HOME}/esp32-iot/secrets.local.txt"
def sec(k):
    for l in open(SEC):
        if l.startswith(k+"="): return l.split("=",1)[1].strip()
    return ""

WAV = sys.argv[1] if len(sys.argv) > 1 else "/tmp/q.wav"
got = {"done": False}

def on_connect(c, u, f, rc, props):
    print(f"✅ 디바이스 HiveMQ 연결 (rc={rc})")
    c.subscribe("daijin/audio/out")
    c.subscribe("daijin/text/out")
    data = open(WAV, "rb").read()
    c.publish("daijin/audio/in", data)
    print(f"📤 질문 음성 발행 {len(data)} bytes → daijin/audio/in")

def on_message(c, u, msg):
    if msg.topic == "daijin/text/out":
        print(f"🤖 daijin(글): {msg.payload.decode('utf-8','replace')}")
    elif msg.topic == "daijin/audio/out":
        out = "/tmp/dev_reply.wav"
        open(out, "wb").write(msg.payload)
        print(f"🔊 답 음성 수신 {len(msg.payload)} bytes → 재생")
        subprocess.run(["afplay", out])
        got["done"] = True

c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="daijin-fakedev")
c.username_pw_set(sec("MQTT_CLOUD_USER"), sec("MQTT_CLOUD_PASS"))
c.tls_set(cert_reqs=ssl.CERT_REQUIRED)
c.on_connect = on_connect
c.on_message = on_message
c.connect(sec("MQTT_CLOUD_HOST"), int(sec("MQTT_CLOUD_PORT")), 60)
c.loop_start()
t0 = time.monotonic()
while not got["done"] and time.monotonic() - t0 < 60:
    time.sleep(0.2)
c.loop_stop()
print("✅ 왕복 완료" if got["done"] else "❌ 타임아웃")
