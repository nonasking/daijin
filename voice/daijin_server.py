#!/usr/bin/env python3
# daijin 브레인 서버 — ESP32가 녹음한 오디오를 받아 AI 음성 답을 돌려줌.
#   POST /talk  (body = WAV 오디오) → whisper(STT) → Claude(두뇌) → say(TTS) → WAV 반환
# 실행:  python3 ~/esp32-iot/voice/daijin_server.py   (포트 8848)
# 테스트: curl -X POST --data-binary @sample.wav http://localhost:8848/talk -o reply.wav

import http.server, subprocess, re, os, time, sys

HOME    = os.path.expanduser("~")
WHISPER = "/opt/homebrew/bin/whisper-cli"
MODEL   = f"{HOME}/esp32-iot/voice/models/ggml-large-v3-turbo-q5_0.bin"
VOICE   = "Yuna"
PORT    = 8848
IN_WAV  = "/tmp/daijin_in.wav"
OUT_WAV = "/tmp/daijin_reply.wav"
SYS = ("너는 'daijin'이라는 이름의 AI 음성 대화 친구야. 따뜻하고 친근하게 한국어로 "
       "2~3문장 이내로 짧게 답해. 이모지·마크다운·특수기호는 쓰지 마(음성으로 읽힘). "
       "집에 제어 가능한 LED가 있어 — 불을 켜/꺼/색 바꿔 달라고 하면 반드시 "
       "'bash ~/esp32-iot/voice/led.sh <색>' 를 실행해(색: red green blue yellow cyan magenta white off; "
       "꺼=off, 켜=green). 실행 후 한국어로 짧게 확인해. LED 요청이 아니면 그냥 대화해.")

EMOJI = re.compile(r"[\U0001F000-\U0001FAFF☀-➿←-⇿*#`_]")
turn = 0

def stt(wav):
    out = subprocess.run([WHISPER, "-m", MODEL, "-l", "ko", "-nt", "-np", "-f", wav],
                         capture_output=True, text=True)
    return out.stdout.strip()

def brain(text):
    global turn
    cmd = ["claude", "-p", text, "--allowedTools", "Bash", "--append-system-prompt", SYS]
    if turn > 0:
        cmd.insert(2, "--continue")
    turn += 1
    out = subprocess.run(cmd, capture_output=True, text=True)
    return EMOJI.sub("", out.stdout).strip()

def tts(text, out):
    if not text:
        text = "잘 못 들었어, 다시 말해줄래?"
    subprocess.run(["say", "-v", VOICE, "-o", out,
                    "--file-format=WAVE", "--data-format=LEI16@16000", text])

class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/talk":
            self.send_error(404); return
        n = int(self.headers.get("Content-Length", 0))
        with open(IN_WAV, "wb") as f:
            f.write(self.rfile.read(n))
        t0 = time.monotonic()
        user = stt(IN_WAV)
        print(f"  🗣️  나: {user}")
        resp = brain(user) if user else "잘 못 들었어, 다시 말해줄래?"
        print(f"  🤖 daijin: {resp}")
        tts(resp, OUT_WAV)
        data = open(OUT_WAV, "rb").read()
        print(f"  ⏱️  {time.monotonic()-t0:.1f}s, reply {len(data)} bytes\n")
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, *a): pass  # 기본 접근로그 끄기

if __name__ == "__main__":
    print(f"🤖 daijin 브레인 서버 :{PORT}  (POST /talk, 종료 Ctrl+C)")
    http.server.HTTPServer(("0.0.0.0", PORT), H).serve_forever()
