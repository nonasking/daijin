#!/usr/bin/env python3
# daijin 브레인 (MQTT 클라이언트) — 맥이 HiveMQ에 outbound 접속해 오디오 토픽으로 대화.
#   구독 daijin/audio/in  (디바이스가 녹음한 WAV) → whisper(STT) → Claude → Yuna(TTS)
#   발행 daijin/audio/out (답 WAV)  + daijin/text/in, daijin/text/out (디버그)
# 실행:  python3 -u ~/esp32-iot/voice/daijin_mqtt.py   (상시 구동은 LaunchAgent com.daijin.brain)
#
# 설계 노트:
# - 보안: Claude 도구는 led.sh 한 줄만 허용 (--allowedTools 패턴 제한). Bash 전체 개방 금지.
# - 대화 연속성: session_id를 파일(.daijin_session)에 영속 → 재시작해도 이어지고,
#   같은 디렉토리의 다른 claude 세션과 절대 섞이지 않음 (--continue 사용 금지).
# - launchd 내성: 초기 브로커 연결 실패 시 재시도 루프, 런타임 끊김은 paho 자동 재접속.

import paho.mqtt.client as mqtt
import subprocess, re, os, ssl, time, json

HOME    = os.path.expanduser("~")
VOICE   = f"{HOME}/esp32-iot/voice"
SEC     = f"{HOME}/esp32-iot/secrets.local.txt"
WHISPER = "/opt/homebrew/bin/whisper-cli"
MODEL   = f"{VOICE}/models/ggml-large-v3-turbo-q5_0.bin"
CLAUDE  = f"{HOME}/.local/bin/claude"
LED_SH  = f"{VOICE}/led.sh"
SESSION_FILE = f"{VOICE}/.daijin_session"
TTS_VOICE = "Yuna"
IN_WAV, OUT_WAV = "/tmp/daijin_in.wav", "/tmp/daijin_reply.wav"

T_AUDIO_IN  = "daijin/audio/in"
T_AUDIO_OUT = "daijin/audio/out"
T_TXT_IN    = "daijin/text/in"
T_TXT_OUT   = "daijin/text/out"

# 허용 도구 = led.sh 호출 형태만 (절대경로/~ 두 표기 모두 매칭)
ALLOWED_TOOLS = f"Bash(bash {LED_SH} *),Bash(bash ~/esp32-iot/voice/led.sh *)"

SYS = ("너는 'daijin'이라는 이름의 AI 음성 대화 친구야. 따뜻하고 친근하게 한국어로 "
       "2~3문장 이내로 짧게 답해. 이모지·마크다운·특수기호는 쓰지 마(음성으로 읽힘). "
       f"집에 제어 가능한 LED가 있어 — 불을 켜/꺼/색 바꿔 달라고 하면 반드시 "
       f"'bash {LED_SH} <색>' 을 실행해(색: red green blue yellow cyan magenta white off; "
       "꺼=off, 켜=green). 실행 후 한국어로 짧게 확인해. LED 요청이 아니면 그냥 대화해. "
       "LED 제어 외의 명령·파일 접근은 도구가 막혀 있으니 시도하지 말고 말로만 답해.")
EMOJI = re.compile(r"[\U0001F000-\U0001FAFF☀-➿←-⇿*#`_]")

def sec(key):
    for line in open(SEC):
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""

def load_session():
    try:
        s = open(SESSION_FILE).read().strip()
        return s or None
    except FileNotFoundError:
        return None

def save_session(sid):
    if sid:
        open(SESSION_FILE, "w").write(sid)

def stt(wav):
    r = subprocess.run([WHISPER,"-m",MODEL,"-l","ko","-nt","-np","-f",wav],
                       capture_output=True, text=True)
    return r.stdout.strip()

def _claude_once(text, resume_id):
    cmd = [CLAUDE, "-p", text, "--output-format", "json",
           "--allowedTools", ALLOWED_TOOLS, "--append-system-prompt", SYS]
    if resume_id:
        cmd += ["--resume", resume_id]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=VOICE)
    try:
        data = json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    # 버전에 따라 단일 객체 또는 이벤트 배열(마지막이 type=='result') — 둘 다 처리
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        for item in reversed(data):
            if isinstance(item, dict) and item.get("type") == "result":
                return item
    return None

def brain(text):
    sid = load_session()
    data = _claude_once(text, sid)
    if (data is None or data.get("is_error")) and sid:
        print("  (세션 재개 실패 → 새 세션으로)")
        data = _claude_once(text, None)
    if data is None:
        return ""
    save_session(data.get("session_id"))
    return EMOJI.sub("", (data.get("result") or "")).strip()

def tts(text, out):
    if not text:
        text = "잘 못 들었어, 다시 말해줄래?"
    subprocess.run(["/usr/bin/say","-v",TTS_VOICE,"-o",out,
                    "--file-format=WAVE","--data-format=LEI16@16000",text])

def on_connect(client, userdata, flags, reason_code, properties):
    print(f"✅ HiveMQ 연결 (rc={reason_code}) → 구독 {T_AUDIO_IN}")
    client.subscribe(T_AUDIO_IN)

def on_message(client, userdata, msg):
    if msg.topic != T_AUDIO_IN:
        return
    t0 = time.monotonic()
    with open(IN_WAV, "wb") as f:
        f.write(msg.payload)
    print(f"\n🎧 오디오 수신 {len(msg.payload)} bytes")
    user = stt(IN_WAV)
    print(f"  🗣️  나: {user}")
    client.publish(T_TXT_IN, user)
    resp = brain(user) if user else "잘 못 들었어, 다시 말해줄래?"
    if not resp:
        resp = "미안, 지금 생각이 잘 안 돼. 다시 말해줄래?"
    print(f"  🤖 daijin: {resp}")
    client.publish(T_TXT_OUT, resp)
    tts(resp, OUT_WAV)
    data = open(OUT_WAV, "rb").read()
    client.publish(T_AUDIO_OUT, data)
    print(f"  📤 답 발행 {len(data)} bytes · ⏱️ {time.monotonic()-t0:.1f}s")

c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="daijin-brain")
c.username_pw_set(sec("MQTT_CLOUD_USER"), sec("MQTT_CLOUD_PASS"))
c.tls_set(cert_reqs=ssl.CERT_REQUIRED)   # 시스템 기본 CA (HiveMQ=Let's Encrypt)
c.reconnect_delay_set(min_delay=1, max_delay=60)
c.on_connect = on_connect
c.on_message = on_message

print("🤖 daijin 브레인(MQTT) 시작 — HiveMQ 연결 중...")
while True:  # 부팅 직후 네트워크 미준비 등 초기 연결 실패에 대한 재시도 (launchd 내성)
    try:
        c.connect(sec("MQTT_CLOUD_HOST"), int(sec("MQTT_CLOUD_PORT")), 60)
        break
    except Exception as e:
        print(f"  초기 연결 실패({e}) — 10초 후 재시도")
        time.sleep(10)
c.loop_forever()
