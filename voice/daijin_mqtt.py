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
import subprocess, re, os, ssl, time, json, wave, io, threading, queue

HOME    = os.path.expanduser("~")
VOICE   = f"{HOME}/esp32-iot/voice"
SEC     = f"{HOME}/esp32-iot/secrets.local.txt"
WHISPER = "/opt/homebrew/bin/whisper-cli"
MODEL   = f"{VOICE}/models/ggml-large-v3-turbo-q5_0.bin"
CLAUDE  = f"{HOME}/.local/bin/claude"
LED_SH  = f"{VOICE}/led.sh"
SESSION_FILE = f"{VOICE}/.daijin_session"
TTS_VOICE = "Yuna"                     # 폴백용 (ElevenLabs 실패 시)
ELEVEN_VOICE = "TFUX5RKA9yUMr26dSJqF"  # daijin 보이스 (Voice Design으로 생성)
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
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=VOICE, stdin=subprocess.DEVNULL)
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

# ---------- 문장 스트리밍: Claude 답을 문장 단위로 실시간 yield ----------
# 전체 답변 완성을 기다리지 않고 첫 문장부터 TTS·재생 → 체감 지연 대폭 감소
_SENT_END = re.compile(r"[.!?…]+[\"')\]]*\s*")

def brain_stream(text):
    def run(resume_id):
        cmd = [CLAUDE, "-p", text, "--output-format", "stream-json",
               "--include-partial-messages", "--verbose",
               "--allowedTools", ALLOWED_TOOLS, "--append-system-prompt", SYS]
        if resume_id:
            cmd += ["--resume", resume_id]
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, text=True, cwd=VOICE)
    sid = load_session()
    attempts = [sid, None] if sid else [None]
    for attempt in attempts:
        p = run(attempt)
        buf, yielded, ok = "", False, False
        for line in p.stdout:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            t = obj.get("type")
            if t == "stream_event":
                ev = obj.get("event", {})
                if ev.get("type") == "content_block_delta" and \
                   ev.get("delta", {}).get("type") == "text_delta":
                    buf += ev["delta"]["text"]
                    while True:
                        m = _SENT_END.search(buf)
                        if not m:
                            break
                        sent = EMOJI.sub("", buf[:m.end()]).strip()
                        buf = buf[m.end():]
                        if sent:
                            yielded = True
                            yield sent
            elif t == "result":
                ok = not obj.get("is_error")
                if ok:
                    save_session(obj.get("session_id"))
        p.wait()
        rest = EMOJI.sub("", buf).strip()
        if rest:
            yielded = True
            yield rest
        if ok or yielded:
            return
        if attempt is not None:
            print("  (세션 재개 실패 → 새 세션으로)")
    yield "미안, 지금 생각이 잘 안 돼. 다시 말해줄래?"

def tts_stream(text):
    """PCM 16k 조각을 생성되는 즉시 yield.
    1차: ElevenLabs flash_v2_5 스트리밍 (같은 보이스, 저지연·크레딧 절반)
    2차 폴백: macOS Yuna (오프라인 보장)"""
    if not text:
        text = "잘 못 들었어, 다시 말해줄래?"
    # secrets.local.txt의 TTS_ENGINE=yuna|eleven 로 전환 (기본 eleven, 브레인 재시작 필요)
    key = sec("ELEVEN_API_KEY")
    if key and sec("TTS_ENGINE") != "yuna":
        try:
            import urllib.request
            req = urllib.request.Request(
                f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}/stream"
                "?output_format=pcm_16000",
                data=json.dumps({"text": text,
                                 "model_id": "eleven_flash_v2_5"}).encode(),
                headers={"xi-api-key": key, "Content-Type": "application/json"})
            resp = urllib.request.urlopen(req, timeout=20)
            first = resp.read(8192)          # 실패면 여기서 예외 → 폴백
            yield first
            while True:
                d = resp.read(8192)
                if not d: break
                yield d
            return
        except Exception as e:
            print(f"  (ElevenLabs 실패: {e} → Yuna 폴백)")
    subprocess.run(["/usr/bin/say","-v",TTS_VOICE,"-o",OUT_WAV,
                    "--file-format=WAVE","--data-format=LEI16@16000",text])
    yield wav_to_pcm(OUT_WAV)

def tts(text, out):
    """통 WAV 경로용 (구형 파이썬 가짜 디바이스 호환)"""
    pcm = b"".join(tts_stream(text))
    pcm_to_wav(pcm, out)

# ---------- 오디오 청킹 (docs/chunking-protocol.md) ----------
# 메시지 = [clipId:1][seq:2 BE][flags:1(bit0=last)][페이로드]
# 다운스트림(audio/out)은 IMA ADPCM 4:1 압축 — EU 브로커 RTT 300ms × ESP32 TCP창 5.7KB
# = 최대 ~19KB/s라 PCM(32KB/s)은 물리적으로 불가. ADPCM은 8KB/s로 여유.
CHUNK = 4096          # 업스트림 PCM 청크 (디바이스→브레인)
CHUNK_OUT = 2048      # 다운스트림 ADPCM 청크 (=4096샘플=256ms)
_rx = {"clip": None, "parts": {}}   # 수신 중인 클립 재조립 버퍼

# ---------- IMA ADPCM 인코더 (16bit PCM → 4bit) ----------
_STEP = [7,8,9,10,11,12,13,14,16,17,19,21,23,25,28,31,34,37,41,45,50,55,60,66,
         73,80,88,97,107,118,130,143,157,173,190,209,230,253,279,307,337,371,
         408,449,494,544,598,658,724,796,876,963,1060,1166,1282,1411,1552,1707,
         1878,2066,2272,2499,2749,3024,3327,3660,4026,4428,4871,5358,5894,6484,
         7132,7845,8630,9493,10442,11487,12635,13899,15289,16818,18500,20350,
         22385,24623,27086,29794,32767]
_IDX = [-1,-1,-1,-1,2,4,6,8]

class AdpcmEnc:
    """스트리밍 인코더 — 조각조각 넣어도 상태(pred/idx/니블·홀수바이트) 유지"""
    def __init__(self):
        self.pred, self.idx, self.lo, self.rem = 0, 0, None, b""

    def encode(self, pcm):
        import array
        data = self.rem + pcm
        odd = len(data) % 2
        if odd: data, self.rem = data[:-1], data[-1:]
        else:   self.rem = b""
        smp = array.array("h"); smp.frombytes(data)
        out = bytearray()
        for s in smp:
            diff = s - self.pred
            code = 8 if diff < 0 else 0
            if code: diff = -diff
            step = _STEP[self.idx]
            if diff >= step:      code |= 4; diff -= step
            if diff >= step >> 1: code |= 2; diff -= step >> 1
            if diff >= step >> 2: code |= 1
            vp = step >> 3
            if code & 4: vp += step
            if code & 2: vp += step >> 1
            if code & 1: vp += step >> 2
            self.pred += -vp if code & 8 else vp
            self.pred = max(-32768, min(32767, self.pred))
            self.idx = max(0, min(88, self.idx + _IDX[code & 7]))
            if self.lo is None: self.lo = code
            else: out.append(self.lo | (code << 4)); self.lo = None
        return bytes(out)

    def flush(self):
        if self.lo is not None:
            b = bytes([self.lo]); self.lo = None
            return b
        return b""

def pcm_to_wav(pcm, path):
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(pcm)

def wav_to_pcm(path):
    with wave.open(path, "rb") as w:
        return w.readframes(w.getnframes())

def publish_pcm_stream(client, pcm_iter):
    """PCM 조각 이터레이터를 받는 즉시 ADPCM 청크로 발행 (TTS 생성과 전송을 겹침)"""
    clip = int(time.time()) & 0xFF
    enc, buf, seq = AdpcmEnc(), b"", 0
    for pcm in pcm_iter:
        buf += enc.encode(pcm)
        while len(buf) >= CHUNK_OUT:
            part, buf = buf[:CHUNK_OUT], buf[CHUNK_OUT:]
            hdr = bytes([clip, (seq >> 8) & 0xFF, seq & 0xFF, 2])      # bit1=ADPCM
            # QoS0 = TCP 스트리밍 (QoS1은 청크마다 EU왕복 ACK에 묶여 재생속도 미달 → 끊김)
            client.publish(T_AUDIO_OUT, hdr + part, qos=0)
            seq += 1
    buf += enc.flush()
    hdr = bytes([clip, (seq >> 8) & 0xFF, seq & 0xFF, 1 | 2])          # 마지막
    client.publish(T_AUDIO_OUT, hdr + buf, qos=0)
    return seq + 1

def on_connect(client, userdata, flags, reason_code, properties):
    print(f"✅ HiveMQ 연결 (rc={reason_code}) → 구독 {T_AUDIO_IN}")
    client.subscribe(T_AUDIO_IN, qos=1)

def handle_clip(client, pcm_or_wav, is_wav):
    t0 = time.monotonic()
    if is_wav:
        with open(IN_WAV, "wb") as f:
            f.write(pcm_or_wav)
    else:
        pcm_to_wav(pcm_or_wav, IN_WAV)
    user = stt(IN_WAV)
    print(f"  🗣️  나: {user}")
    client.publish(T_TXT_IN, user)
    if is_wav:  # 구형(파이썬 가짜 디바이스): 통 WAV로 응답
        resp = brain(user) if user else "잘 못 들었어, 다시 말해줄래?"
        if not resp:
            resp = "미안, 지금 생각이 잘 안 돼. 다시 말해줄래?"
        print(f"  🤖 daijin: {resp}")
        client.publish(T_TXT_OUT, resp)
        tts(resp, OUT_WAV)
        data = open(OUT_WAV, "rb").read()
        client.publish(T_AUDIO_OUT, data)
        print(f"  📤 답 발행(통WAV) {len(data)} bytes · ⏱️ {time.monotonic()-t0:.1f}s")
    else:       # 신형(ESP32): 문장 완성 즉시 TTS→발행 (완전 스트리밍 파이프라인)
        sentences = []
        def gen():
            src = brain_stream(user) if user else iter(["잘 못 들었어, 다시 말해줄래?"])
            for s in src:
                sentences.append(s)
                print(f"  🤖 daijin: {s}  (+{time.monotonic()-t0:.1f}s)")
                for pcm in tts_stream(s):
                    yield pcm
                yield b"\x00" * 3200          # 문장 사이 0.1초 숨고르기
        n = publish_pcm_stream(client, gen())
        client.publish(T_TXT_OUT, " ".join(sentences))
        print(f"  📤 답 발행 {n} 청크 · ⏱️ {time.monotonic()-t0:.1f}s")

# 처리(whisper/Claude/TTS/발행)는 워커 스레드에서 — paho 네트워크 스레드(콜백)에서 하면
# publish가 콜백 종료까지 실제 송신되지 않아 스트리밍이 무효화됨
_clipq = queue.Queue()

def worker(client):
    while True:
        pcm_or_wav, is_wav = _clipq.get()
        try:
            handle_clip(client, pcm_or_wav, is_wav)
        except Exception as e:
            print(f"  ⚠️ 처리 오류: {e}")

def on_message(client, userdata, msg):
    if msg.topic != T_AUDIO_IN:
        return
    p = msg.payload
    if p[:4] == b"RIFF":                      # 구형: 통 WAV 한 방
        print(f"\n🎧 통WAV 수신 {len(p)} bytes")
        _clipq.put((p, True))
        return
    if len(p) <= 4:
        return
    clip, seq, last = p[0], (p[1] << 8) | p[2], p[3] & 1
    if _rx["clip"] != clip:                   # 새 클립 시작
        _rx["clip"], _rx["parts"] = clip, {}
    _rx["parts"][seq] = p[4:]
    if last:
        pcm = b"".join(_rx["parts"][k] for k in sorted(_rx["parts"]))
        print(f"\n🎧 클립 {clip} 수신: {len(_rx['parts'])} 청크, {len(pcm)} bytes")
        _rx["clip"], _rx["parts"] = None, {}
        _clipq.put((pcm, False))

c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="daijin-brain")
c.username_pw_set(sec("MQTT_CLOUD_USER"), sec("MQTT_CLOUD_PASS"))
c.tls_set(cert_reqs=ssl.CERT_REQUIRED)   # 시스템 기본 CA (HiveMQ=Let's Encrypt)
c.reconnect_delay_set(min_delay=1, max_delay=60)
c.on_connect = on_connect
c.on_message = on_message

threading.Thread(target=worker, args=(c,), daemon=True).start()

print("🤖 daijin 브레인(MQTT) 시작 — HiveMQ 연결 중...")
while True:  # 부팅 직후 네트워크 미준비 등 초기 연결 실패에 대한 재시도 (launchd 내성)
    try:
        c.connect(sec("MQTT_CLOUD_HOST"), int(sec("MQTT_CLOUD_PORT")), 60)
        break
    except Exception as e:
        print(f"  초기 연결 실패({e}) — 10초 후 재시도")
        time.sleep(10)
c.loop_forever()
