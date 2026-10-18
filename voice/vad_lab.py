#!/usr/bin/env python3
# 기기의 "말이 끝났나" 판정(에너지 VAD, sketches/14-daijin/14-daijin.ino 의 vadBlock)을 맞추는 실험대.
#
#   record [--out DIR]       기기가 daijin/audio/in 으로 보내는 클립을 받아 WAV 로 저장하고, 펌웨어 VAD 를 흉내 내 어디서 끊었을지 찍는다
#   sim WAV... [--variant V]  저장한 클립에 VAD 변형들을 돌려 비교한다 (어디서 끊는지, 말 도중인지)
#   trace WAV                 32ms 블록마다 RMS 와 판정을 보여 준다
#
# 펌웨어와 같은 단위로 본다: 16kHz, 512 샘플(32ms) 블록의 RMS, x2 게인이 들어간 16비트 값 (클립이 바로 그 샘플이다, ADPCM 오차만 있다).
# 녹음은 사람 목소리이므로 저장소 밖(기본 voice/.vadlab/, gitignore)에 둔다.
import argparse, array, json, math, os, sys, time, wave

SR = 16000
BLOCK = 512                       # 펌웨어 captureTask 의 한 번 읽기 (32ms)
BLOCK_MS = BLOCK * 1000 // SR
REC_MIN_S = 1.0                   # 펌웨어 REC_MIN_B: 1초 전에는 끝내지 않는다

# 변형들. 펌웨어를 바꾸면 여기에 새 변형을 더하고 DEFAULT 를 옮긴다 (tests/test_vad_lab.py 가 펌웨어 상수와 맞는지 본다).
#   vad1 = 처음 넣은 VAD (2026-10-08, 0.7초), vad2 = 실제 대화 녹음으로 다시 맞춘 것 (1.5초)
#   on_min/off_min: 말소리·무음 문턱의 바닥값 (바닥 소음의 배수와 비교해 큰 쪽을 쓴다)
#   consecutive: True 면 무음 문턱을 넘는 블록(말소리든 그 사이든)이 무음 누적을 지운다 (연속된 무음만 센다)
#   floor_from: 바닥을 끌어올리는 블록. below_on = 말소리 문턱 아래 전부(작은 말소리 포함), below_off = 무음 문턱 아래만
VARIANTS = {
    "vad1": dict(speech_ms=300, silence_ms=700, on_min=400.0, off_min=240.0, on_mult=4.0, off_mult=2.5,
                          consecutive=False, floor_from="below_on"),
    # 탭으로 끝낸 대화 녹음 10개: 말 도중 끊김 7 → 0 (근거는 펌웨어 주석)
    "vad2": dict(speech_ms=300, silence_ms=1500, on_min=150.0, off_min=80.0, on_mult=4.0, off_mult=2.5,
                          consecutive=True, floor_from="below_off"),
}
DEFAULT = "vad2"

def block_rms(pcm):
    a = array.array("h"); a.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    out = []
    for i in range(0, len(a) - BLOCK + 1, BLOCK):
        seg = a[i:i + BLOCK]
        out.append(math.sqrt(sum(x * x for x in seg) / BLOCK))
    return out

def simulate(rms, speech_ms, silence_ms, on_min, off_min, on_mult, off_mult, consecutive, floor_from="below_on", trace=None):
    """펌웨어 vadBlock + 끝내기 조건을 그대로 따라간다. 끊는 블록 번호(그 블록 끝에서 녹음이 끝남) 또는 None.
    consecutive=False 가 지금 펌웨어: 문턱 사이(말소리도 무음도 아닌) 블록은 무음 누적을 지우지 않는다."""
    floor = 0.0; speech = silence = 0
    for i, r in enumerate(rms):
        on_th = max(on_min, floor * on_mult)
        off_th = max(off_min, floor * off_mult)
        if floor <= 0 or r < floor:
            floor = r
        elif r < (on_th if floor_from == "below_on" else off_th):
            floor += (r - floor) * 0.05        # below_on: 말소리 문턱 아래면 무엇이든(작은 말소리도) 바닥을 끌어올린다 (지금 펌웨어)
        if r > on_th:
            speech += BLOCK_MS; silence = 0; state = "#"
        elif r < off_th:
            silence += BLOCK_MS; state = "."
        else:
            state = "+"
            if consecutive:
                silence = 0
        if trace is not None:
            trace.append((i, r, state, round(floor), round(on_th), round(off_th), speech, silence))
        if (i + 1) * BLOCK / SR >= REC_MIN_S and speech >= speech_ms and silence >= silence_ms:
            return i
    return None

def speech_extent(rms, level):
    """level 을 넘는 블록의 처음과 끝 (사람이 실제로 말한 범위를 어림한다)."""
    idx = [i for i, r in enumerate(rms) if r > level]
    return (idx[0], idx[-1]) if idx else (None, None)

def read_wav(path):
    with wave.open(path, "rb") as w:
        return w.readframes(w.getnframes())

def write_wav(path, pcm):
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm)

def mid_speech(rms, stop, level=150, blocks=3):
    """끊은 뒤에도 말소리 블록이 이어지면 True (말 도중에 끊었다). 끝의 탭 딸깍 소리(마지막 3블록)는 뺀다."""
    return stop is not None and sum(1 for r in rms[stop + 1:-3] if r > level) >= blocks

def summarize(name, rms, variant=DEFAULT, **override):
    stop = simulate(rms, **dict(VARIANTS[variant], **override))
    dur = len(rms) * BLOCK_MS / 1000
    srt = sorted(rms)
    p = lambda q: round(srt[min(len(srt) - 1, int(q * len(srt)))]) if srt else 0
    cut = (f"{(stop + 1) * BLOCK_MS / 1000:.2f}s 에서 끊음" + (" (말 도중!)" if mid_speech(rms, stop) else "")) if stop is not None else "끊지 않음"
    return f"{name}: {dur:.2f}s · RMS p10 {p(.1)} p50 {p(.5)} p90 {p(.9)} max {p(1)} · {variant}: {cut}"

# ---------- record ----------
def record(out_dir):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from daijin_common import cloud_client, connect_with_retry
    from daijin_mqtt import AdpcmDec, T_AUDIO_IN
    os.makedirs(out_dir, exist_ok=True)
    rx = {"clip": None, "parts": {}, "adpcm": False}

    def on_message(c, u, msg):
        p = msg.payload
        if len(p) <= 4:
            return
        clip, seq, last = p[0], (p[1] << 8) | p[2], p[3] & 1
        if rx["clip"] != clip or seq == 0:
            rx.update(clip=clip, parts={}, adpcm=bool(p[3] & 2))
        rx["parts"][seq] = p[4:]
        if not last:
            return
        data = b"".join(rx["parts"][k] for k in sorted(rx["parts"]))
        missing = (max(rx["parts"]) + 1) - len(rx["parts"])
        pcm = AdpcmDec().decode(data) if rx["adpcm"] else data
        name = time.strftime("%H%M%S") + f"-clip{clip:03d}"
        write_wav(os.path.join(out_dir, name + ".wav"), pcm)
        print(summarize(name, block_rms(pcm)) + (f" · 빠진 청크 {missing}" if missing else ""), flush=True)
        rx.update(clip=None, parts={})

    c = cloud_client("daijin-vadlab")
    c.on_connect = lambda cl, u, f, rc, pr=None: cl.subscribe(T_AUDIO_IN, qos=1)
    c.on_message = on_message
    connect_with_retry(c)
    print(f"녹음 대기: {out_dir} (Ctrl-C 로 끝)", flush=True)
    try:
        c.loop_forever()
    except KeyboardInterrupt:
        pass

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record"); r.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".vadlab"))
    s = sub.add_parser("sim"); s.add_argument("wavs", nargs="+"); s.add_argument("--variant", action="append")
    s.add_argument("--silence-ms", type=int, help="변형의 무음 시간만 바꿔 본다")
    t = sub.add_parser("trace"); t.add_argument("wav"); t.add_argument("--variant", default=DEFAULT)
    a = ap.parse_args(argv)
    if a.cmd == "record":
        record(a.out)
    elif a.cmd == "sim":
        over = {"silence_ms": a.silence_ms} if a.silence_ms else {}
        for v in (a.variant or list(VARIANTS)):
            print(f"== {v}" + (f" (무음 {a.silence_ms}ms)" if over else ""))
            mids = 0
            for w in a.wavs:
                rms = block_rms(read_wav(w))
                mids += mid_speech(rms, simulate(rms, **dict(VARIANTS[v], **over)))
                print("  " + summarize(os.path.basename(w), rms, v, **over))
            print(f"  → 말 도중 끊음 {mids}/{len(a.wavs)}")
    elif a.cmd == "trace":
        tr = []
        stop = simulate(block_rms(read_wav(a.wav)), **VARIANTS[a.variant], trace=tr)
        for i, r, st, fl, on, off, sp, si in tr:
            print(f"{i * BLOCK_MS / 1000:5.2f}s {st} rms {r:6.0f}  바닥 {fl:5d}  말>{on:5d}  무음<{off:5d}  말누적 {sp:5d}  무음누적 {si:4d}")
        print("끊음:", f"{(stop + 1) * BLOCK_MS / 1000:.2f}s" if stop is not None else "없음")

if __name__ == "__main__":
    main()
