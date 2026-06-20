#!/usr/bin/env python3
# daijin 브레인 (MQTT 클라이언트) — 맥이 HiveMQ에 outbound 접속해 오디오 토픽으로 대화.
#   구독 daijin/audio/in  (디바이스가 녹음한 WAV) → whisper(STT) → Claude → Yuna(TTS)
#   발행 daijin/audio/out (답 WAV)  + daijin/text/in, daijin/text/out (디버그)
# 실행:  python3 -u ~/esp32-iot/voice/daijin_mqtt.py   (종료 Ctrl+C)

import paho.mqtt.client as mqtt
import subprocess, re, os, ssl, time

HOME    = os.path.expanduser("~")
SEC     = f"{HOME}/esp32-iot/secrets.local.txt"
WHISPER = "/opt/homebrew/bin/whisper-cli"
MODEL   = f"{HOME}/esp32-iot/voice/models/ggml-large-v3-turbo-q5_0.bin"
VOICE   = "Yuna"
IN_WAV, OUT_WAV = "/tmp/daijin_in.wav", "/tmp/daijin_reply.wav"

T_AUDIO_IN  = "daijin/audio/in"
T_AUDIO_OUT = "daijin/audio/out"
T_TXT_IN    = "daijin/text/in"
T_TXT_OUT   = "daijin/text/out"

SYS = ("너는 'daijin'이라는 이름의 AI 음성 대화 친구야. 따뜻하고 친근하게 한국어로 "
       "2~3문장 이내로 짧게 답해. 이모지·마크다운·특수기호는 쓰지 마(음성으로 읽힘). "
       "집에 제어 가능한 LED가 있어 — 불을 켜/꺼/색 바꿔 달라고 하면 반드시 "
       "'bash ~/esp32-iot/voice/led.sh <색>' 를 실행해(색: red green blue yellow cyan magenta white off; "
       "꺼=off, 켜=green). 실행 후 한국어로 짧게 확인해. LED 요청이 아니면 그냥 대화해.")
EMOJI = re.compile(r"[\U0001F000-\U0001FAFF☀-➿←-⇿*#`_]")

def sec(key):
    for line in open(SEC):
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""

turn = 0
def stt(wav):
    r = subprocess.run([WHISPER,"-m",MODEL,"-l","ko","-nt","-np","-f",wav], capture_output=True, text=True)
    return r.stdout.strip()
def brain(text):
    global turn
    cmd = ["claude","-p",text,"--allowedTools","Bash","--append-system-prompt",SYS]
    if turn > 0: cmd.insert(2,"--continue")
    turn += 1
    r = subprocess.run(cmd, capture_output=True, text=True)
    return EMOJI.sub("", r.stdout).strip()
def tts(text, out):
    if not text: text = "잘 못 들었어, 다시 말해줄래?"
    subprocess.run(["say","-v",VOICE,"-o",out,"--file-format=WAVE","--data-format=LEI16@16000",text])

def on_connect(client, userdata, flags, reason_code, properties):
    print(f"✅ HiveMQ 연결 (rc={reason_code}) → 구독 {T_AUDIO_IN}")
    client.subscribe(T_AUDIO_IN)

def on_message(client, userdata, msg):
    if msg.topic != T_AUDIO_IN: return
    t0 = time.monotonic()
    with open(IN_WAV,"wb") as f: f.write(msg.payload)
    print(f"\n🎧 오디오 수신 {len(msg.payload)} bytes")
    user = stt(IN_WAV)
    print(f"  🗣️  나: {user}")
    client.publish(T_TXT_IN, user)
    resp = brain(user) if user else "잘 못 들었어, 다시 말해줄래?"
    print(f"  🤖 daijin: {resp}")
    client.publish(T_TXT_OUT, resp)
    tts(resp, OUT_WAV)
    data = open(OUT_WAV,"rb").read()
    client.publish(T_AUDIO_OUT, data)
    print(f"  📤 답 발행 {len(data)} bytes · ⏱️ {time.monotonic()-t0:.1f}s")

c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="daijin-brain")
c.username_pw_set(sec("MQTT_CLOUD_USER"), sec("MQTT_CLOUD_PASS"))
c.tls_set(cert_reqs=ssl.CERT_REQUIRED)   # 시스템 기본 CA (HiveMQ=Let's Encrypt)
c.on_connect = on_connect
c.on_message = on_message
print("🤖 daijin 브레인(MQTT) 시작 — HiveMQ 연결 중...")
c.connect(sec("MQTT_CLOUD_HOST"), int(sec("MQTT_CLOUD_PORT")), 60)
c.loop_forever()
