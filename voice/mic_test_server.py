#!/usr/bin/env python3
# mic_test_server — ESP32가 POST한 녹음 WAV를 저장하고 whisper로 받아쓰기 출력.
# 실행:  python3 -u ~/esp32-iot/voice/mic_test_server.py   (포트 8849)
import http.server, subprocess, os
HOME=os.path.expanduser("~")
WHISPER="/opt/homebrew/bin/whisper-cli"
MODEL=f"{HOME}/esp32-iot/voice/models/ggml-large-v3-turbo-q5_0.bin"
WAV="/tmp/mic_test.wav"
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n=int(self.headers.get("Content-Length",0))
        open(WAV,"wb").write(self.rfile.read(n))
        print(f"\n🎧 수신 {n} bytes → whisper 받아쓰기...")
        r=subprocess.run([WHISPER,"-m",MODEL,"-l","ko","-nt","-np","-f",WAV],
                         capture_output=True,text=True)
        txt=r.stdout.strip()
        print(f"  📝 받아쓴 내용: {txt or '(빈 결과 — 무음/너무 작음?)'}")
        print(f"  ▶︎ 들어보려면: afplay {WAV}")
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self,*a): pass
if __name__=="__main__":
    print("🎤 mic_test_server :8849  (ESP32 POST 대기, Ctrl+C 종료)")
    http.server.HTTPServer(("0.0.0.0",8849),H).serve_forever()
