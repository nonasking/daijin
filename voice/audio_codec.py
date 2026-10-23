#!/usr/bin/env python3
# 오디오 코덱: IMA ADPCM (16bit PCM ↔ 4bit, 4:1) 과 WAV. 펌웨어 sketches/14-daijin 의 adpcmEncode/디코더와 짝이다.
# 브레인(daijin_mqtt), 가짜 디바이스(test_device), VAD 실험대(vad_lab)가 같이 쓴다. 브로커·STT·TTS 에 기대지 않는다.
import array, wave

# ---------- IMA ADPCM 인코더 (16bit PCM → 4bit) ----------
_STEP = [7,8,9,10,11,12,13,14,16,17,19,21,23,25,28,31,34,37,41,45,50,55,60,66,
         73,80,88,97,107,118,130,143,157,173,190,209,230,253,279,307,337,371,
         408,449,494,544,598,658,724,796,876,963,1060,1166,1282,1411,1552,1707,
         1878,2066,2272,2499,2749,3024,3327,3660,4026,4428,4871,5358,5894,6484,
         7132,7845,8630,9493,10442,11487,12635,13899,15289,16818,18500,20350,
         22385,24623,27086,29794,32767]
_IDX = [-1,-1,-1,-1,2,4,6,8]

class AdpcmEnc:
    """스트리밍 인코더 — 조각조각 넣어도 상태(pred/idx/니블·홀수바이트) 유지.
    vol: 디지털 음량(0~1) — 작은 앰프/스피커의 풀스케일 왜곡(쇳소리) 방지"""
    def __init__(self, vol=1.0):
        self.pred, self.idx, self.lo, self.rem = 0, 0, None, b""
        self.vol = vol

    def encode(self, pcm):
        data = self.rem + pcm
        odd = len(data) % 2
        if odd: data, self.rem = data[:-1], data[-1:]
        else:   self.rem = b""
        smp = array.array("h"); smp.frombytes(data)
        if self.vol < 0.999:
            for i in range(len(smp)):
                smp[i] = int(smp[i] * self.vol)
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

class AdpcmDec:
    """업스트림(디바이스 녹음) 디코더 — 펌웨어 adpcmEncode의 거울.
    업로드도 다운로드와 같은 대역 한계(~19KB/s)라 디바이스가 4:1 압축해 보낸다 (2026-08-17)."""
    def __init__(self):
        self.pred, self.idx = 0, 0

    def decode(self, data):
        out = array.array("h")
        for b in data:
            for c in (b & 0x0F, b >> 4):
                step = _STEP[self.idx]
                vp = step >> 3
                if c & 4: vp += step
                if c & 2: vp += step >> 1
                if c & 1: vp += step >> 2
                self.pred += -vp if c & 8 else vp
                self.pred = max(-32768, min(32767, self.pred))
                self.idx = max(0, min(88, self.idx + _IDX[c & 7]))
                out.append(self.pred)
        return out.tobytes()

def pcm_to_wav(pcm, path):
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(pcm)

def wav_to_pcm(path):
    with wave.open(path, "rb") as w:
        return w.readframes(w.getnframes())
