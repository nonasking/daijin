#!/usr/bin/env python3
# 가짜 디바이스 — ESP32 없이 브레인까지 전 구간을 시험한다. 실제 디바이스와 같은 청크 프로토콜을 쓴다
# (docs/chunking-protocol.md): 질문 음성을 IMA ADPCM 청크로 daijin/audio/in 에 올리고,
# daijin/audio/out 으로 오는 답 청크를 받아 디코드해 WAV 로 저장한다. 맥·리눅스 공통.
#
# 사용:  python3 voice/test_device.py 질문.wav            16kHz 모노 WAV (다른 형식이면 ffmpeg 로 맞춘다)
#        python3 voice/test_device.py --say "레드 모터 돌려 줘"   질문 음성을 TTS 로 만들어 보낸다 (녹음 파일이 없을 때)
#        옵션: --out 답.wav (기본 /tmp/daijin_reply.wav) · --play 받은 답을 스피커로 · --timeout 초 (기본 90)
# 주의: 진짜 브레인이 듣고 진짜로 행동한다. 대화 기억에도 남는다.
import argparse, os, shutil, subprocess, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import daijin_mqtt as dm
from daijin_common import cloud_client, connect_with_retry

T_IN, T_OUT = dm.T_AUDIO_IN, dm.T_AUDIO_OUT
CHUNK_UP = 2048                     # 디바이스 펌웨어와 같은 ADPCM 청크 크기

def question_pcm(args):
    if args.say:
        return b"".join(dm.tts_stream(args.say))
    path = args.wav
    with tempfile.TemporaryDirectory() as d:
        try:
            import wave
            with wave.open(path, "rb") as w:
                ok = (w.getnchannels(), w.getframerate(), w.getsampwidth()) == (1, 16000, 2)
        except Exception:
            ok = False
        if not ok:                  # 형식이 다르면 16k 모노 16bit 로 맞춘다
            fixed = f"{d}/q.wav"
            subprocess.run([dm.FFMPEG, "-loglevel", "error", "-y", "-i", path, "-ac", "1", "-ar", "16000",
                            "-sample_fmt", "s16", fixed], check=True)
            path = fixed
        return dm.wav_to_pcm(path)

def clip_chunks(pcm, clip_id):
    """PCM → 디바이스가 보내는 것과 같은 메시지들: [clipId][seq:2 BE][flags: bit0=마지막, bit1=ADPCM][ADPCM ≤2048B]"""
    enc = dm.AdpcmEnc()
    data = enc.encode(pcm) + enc.flush()
    parts = [data[i:i + CHUNK_UP] for i in range(0, len(data), CHUNK_UP)] or [b""]
    for seq, part in enumerate(parts):
        last = 1 if seq == len(parts) - 1 else 0
        yield bytes([clip_id, (seq >> 8) & 0xFF, seq & 0xFF, last | 2]) + part

def main():
    ap = argparse.ArgumentParser(description="daijin 가짜 디바이스 (전 구간 왕복 시험)")
    ap.add_argument("wav", nargs="?", help="질문 음성 WAV")
    ap.add_argument("--say", help="질문을 글로 주면 TTS 로 음성을 만들어 보낸다")
    ap.add_argument("--out", default=os.path.join(tempfile.gettempdir(), "daijin_reply.wav"))
    ap.add_argument("--play", action="store_true", help="받은 답을 재생 (afplay / aplay / ffplay)")
    ap.add_argument("--timeout", type=float, default=90)
    args = ap.parse_args()
    if not args.wav and not args.say:
        ap.error("질문 WAV 나 --say 중 하나가 필요하다")

    pcm = question_pcm(args)
    done = threading.Event()
    st = {"dec": None, "pcm": bytearray(), "chunks": 0, "t_sent": None, "t_first": None, "empty": False}

    def on_connect(c, u, f, rc, props):
        c.subscribe([(T_OUT, 0), (dm.T_TXT_IN, 0), (dm.T_TXT_OUT, 0)])

    def on_subscribe(c, u, mid, rcs, props):
        msgs = list(clip_chunks(pcm, int(time.time()) & 0xFF))
        for m in msgs:
            c.publish(T_IN, m, qos=0)
        st["t_sent"] = time.monotonic()
        print(f"📤 질문 {len(pcm) / 32000:.1f}초 → {len(msgs)} 청크 발행")

    def on_message(c, u, msg):
        if msg.topic == dm.T_TXT_IN:
            print(f"🗣️  들린 말: {msg.payload.decode('utf-8', 'replace')}")
        elif msg.topic == dm.T_TXT_OUT:
            print(f"🤖 답(글): {msg.payload.decode('utf-8', 'replace')}")
        elif msg.topic == T_OUT and len(msg.payload) >= 4 and st["t_sent"]:
            p = msg.payload
            seq, flags = (p[1] << 8) | p[2], p[3]
            if seq == 0:                                   # 새 클립 = 디코더 상태 리셋 (펌웨어와 동일)
                st["dec"] = dm.AdpcmDec()
            if st["t_first"] is None and len(p) > 4:
                st["t_first"] = time.monotonic()
            if st["dec"] is not None and len(p) > 4:
                st["pcm"] += st["dec"].decode(p[4:]) if flags & 2 else p[4:]
            st["chunks"] += 1
            if flags & 1:
                st["empty"] = not st["pcm"]
                done.set()

    c = cloud_client("daijin-fakedev")
    c.on_connect, c.on_subscribe, c.on_message = on_connect, on_subscribe, on_message
    connect_with_retry(c)
    c.loop_start()
    ok = done.wait(args.timeout)
    time.sleep(0.5)                                         # 뒤따라오는 text/out 을 받을 여유
    c.loop_stop(); c.disconnect()
    if not ok:
        print("❌ 타임아웃: 답이 오지 않았다 (브레인이 떠 있는지, 로그를 확인)"); return 1
    if st["empty"]:
        print("🔇 브레인이 '답할 것 없음' 신호를 보냈다 (무음·환청으로 판정된 클립)"); return 0
    dm.pcm_to_wav(bytes(st["pcm"]), args.out)
    print(f"🔊 답 {len(st['pcm']) / 32000:.1f}초, {st['chunks']} 청크 → {args.out}"
          f" · 첫 소리까지 {st['t_first'] - st['t_sent']:.1f}초")
    if args.play:
        for player in (["afplay"], ["aplay", "-q"], ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]):
            if shutil.which(player[0]):
                subprocess.run(player + [args.out]); break
    return 0

if __name__ == "__main__":
    sys.exit(main())
