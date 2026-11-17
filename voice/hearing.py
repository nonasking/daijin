#!/usr/bin/env python3
# hearing — 들은 소리가 말인지, 누구 목소리인지. 브레인이 클립을 받을 때마다 쓴다.
#   1) 말소리의 양 (Silero VAD): 기기는 소리 크기만 본다. 문 닫는 소리, 설거지, 키보드도 문턱을 넘으면 클립이 된다.
#      대화 모드에서 말소리가 거의 없는 클립은 음성 인식 전에 버린다 (daijin_mqtt.handle_clip).
#      TV 와 라디오는 못 거른다: 그것도 사람 말소리다. 그건 2) 가 가늠한다.
#   2) 목소리 닮음 (화자 임베딩): 등록해 둔 목소리와 클립이 얼마나 닮았나 (코사인, -1~1).
#      지금은 재 보기만 한다 (2026-11-17): 점수를 턴 기록에 남길 뿐 동작은 바꾸지 않는다. 내 말과 TV·다른 사람 말의 점수가
#      실제로 갈리는지 본 뒤에 다이진에게 알려 줄지 정한다. 짧은 말(2초 아래)은 점수가 흔들린다: 그래서 말소리 길이도 같이 남긴다.
# 둘 다 sherpa-onnx 로 돈다 (서버 venv: pip install sherpa-onnx). 모델은 voice/models/ 에 직접 받는다 (install 은 이 파일 맨 아래 명령).
# sherpa-onnx 나 모델이 없으면 전부 None 을 돌려주고 브레인은 예전처럼 돈다.
# 목소리 지문은 selfdb 의 voices 표에 (임베딩 평균), 등록에 쓴 녹음은 voice/voiceprint/ 에 남는다 (모델을 바꾸면 다시 계산하려고. git 에 안 들어간다).
#
# 실행: python3 hearing.py report [일수]     최근 클립의 말소리 길이·닮음 점수를 버튼/대화 모드별로
#       python3 hearing.py wav 파일.wav       그 파일의 말소리 길이와 닮음 점수
#       python3 hearing.py reenroll           voiceprint/ 의 녹음으로 지문을 다시 계산 (모델을 바꿨을 때)
#       python3 hearing.py forget             지문과 등록 녹음을 지운다
import array, json, os, sys, threading, time
from daijin_common import VOICE, sec
import selfdb

SR = 16000
MODELS = os.path.join(VOICE, "models")
VAD_MODEL = os.path.join(MODELS, "silero_vad.onnx")
PRINT_DIR = os.path.join(VOICE, "voiceprint")
ME = "me"                              # 지금은 한 사람만 등록한다
ENROLL_CLIPS = 5                       # 지문 하나에 쓰는 말의 수
ENROLL_MIN_SPEECH_S = 2.0              # 이보다 짧은 말은 지문에 넣지 않는다
MIN_SPEECH_S = 0.4                     # 대화 모드: 말소리가 이보다 적으면 말이 아니다
_lock = threading.Lock()               # sherpa 객체는 스레드 하나씩만 쓴다 (턴 스레드와 배경 채점이 겹칠 수 있다)
_vad = _ext = None
_failed = set()

def speaker_model():
    return os.path.join(MODELS, sec("VOICE_MODEL") or "wespeaker_en_voxceleb_resnet34_LM.onnx")

def _samples(pcm):
    """16비트 PCM 바이트 → -1~1 float32 numpy 배열."""
    import numpy as np
    return np.frombuffer(pcm[:len(pcm) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0

def _load(kind):
    """sherpa 객체를 한 번만 만든다. 없으면 None (한 번 실패하면 다시 시도하지 않는다)."""
    global _vad, _ext
    if kind in _failed:
        return None
    try:
        import sherpa_onnx
        if kind == "vad":
            if _vad is None:
                c = sherpa_onnx.VadModelConfig()
                c.silero_vad.model = VAD_MODEL
                c.silero_vad.threshold = 0.5
                c.silero_vad.min_silence_duration = 0.25
                c.silero_vad.min_speech_duration = 0.1
                c.sample_rate = SR
                if not os.path.exists(VAD_MODEL):
                    raise FileNotFoundError(VAD_MODEL)
                _vad = (sherpa_onnx, c)
            return _vad
        if _ext is None:
            if not os.path.exists(speaker_model()):
                raise FileNotFoundError(speaker_model())
            _ext = sherpa_onnx.SpeakerEmbeddingExtractor(sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=speaker_model(), num_threads=1))
        return _ext
    except Exception as e:
        _failed.add(kind)
        print(f"  (hearing: {kind} 를 쓸 수 없다 → 예전처럼 돈다: {e!r})")
        return None

def speech(pcm):
    """클립에서 말소리만: (말소리 초, 말소리 PCM float 배열). VAD 를 쓸 수 없으면 (None, None)."""
    with _lock:
        v = _load("vad")
        if v is None:
            return None, None
        import numpy as np
        sherpa_onnx, cfg = v
        vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=30)      # 클립마다 새로: 상태가 다음 클립으로 넘어가지 않게
        x, w, parts = _samples(pcm), cfg.silero_vad.window_size, []
        for i in range(0, len(x) - w + 1, w):
            vad.accept_waveform(x[i:i + w])
            while not vad.empty():
                parts.append(np.array(vad.front.samples, dtype=np.float32)); vad.pop()
        vad.flush()
        while not vad.empty():
            parts.append(np.array(vad.front.samples, dtype=np.float32)); vad.pop()
        if not parts:
            return 0.0, np.zeros(0, dtype=np.float32)
        s = np.concatenate(parts)
        return round(len(s) / SR, 2), s

def embed(samples):
    """말소리 float 배열 → 길이 1 로 맞춘 임베딩 (numpy). 쓸 수 없거나 너무 짧으면 None."""
    if samples is None or len(samples) < SR // 2:
        return None
    with _lock:
        ex = _load("speaker")
        if ex is None:
            return None
        import numpy as np
        st = ex.create_stream()
        st.accept_waveform(sample_rate=SR, waveform=samples)
        st.input_finished()
        e = np.array(ex.compute(st), dtype=np.float32)
        n = float(np.linalg.norm(e))
        return e / n if n > 0 else None

# ---------- 지문 ----------
def voiceprint(name=ME):
    """등록된 목소리의 평균 임베딩 (numpy) 또는 None. 다른 모델로 만든 지문은 쓰지 않는다."""
    row = selfdb.voice_get(name)
    if not row or row["model"] != os.path.basename(speaker_model()):
        return None
    import numpy as np
    return np.frombuffer(row["emb"], dtype=np.float32)

def score(samples, name=ME):
    """말소리가 등록된 목소리와 얼마나 닮았나 (코사인). 지문이 없거나 잴 수 없으면 None."""
    vp = voiceprint(name)
    if vp is None:
        return None
    e = embed(samples)
    if e is None or len(e) != len(vp):
        return None
    return round(float(e @ vp), 3)

def _save_print(embs, name=ME):
    import numpy as np
    m = np.mean(np.stack(embs), axis=0)
    m = (m / np.linalg.norm(m)).astype(np.float32)
    selfdb.voice_set(name, os.path.basename(speaker_model()), len(embs), m.tobytes())

# ---------- 등록: 버튼을 누르고 한 말 ENROLL_CLIPS 개 ----------
_enroll = {"left": 0, "embs": []}

def enroll_start():
    _enroll.update(left=ENROLL_CLIPS, embs=[])
    os.makedirs(PRINT_DIR, exist_ok=True)
    for f in os.listdir(PRINT_DIR):
        if f.startswith(ME + "-") and f.endswith(".pcm"):
            os.remove(os.path.join(PRINT_DIR, f))

def enrolling():
    return _enroll["left"] > 0

def enroll_add(pcm, samples, speech_s):
    """등록 중에 온 버튼 클립 하나. 돌려주는 값: ("short"|"failed"|"more"|"done", 남은 수)."""
    if speech_s is None or speech_s < ENROLL_MIN_SPEECH_S:
        return "short", _enroll["left"]
    e = embed(samples)
    if e is None:
        return "failed", _enroll["left"]
    _enroll["embs"].append(e); _enroll["left"] -= 1
    k = ENROLL_CLIPS - _enroll["left"]
    with open(os.path.join(PRINT_DIR, f"{ME}-{k}.pcm"), "wb") as f:      # 원래 클립 그대로 (16kHz 16비트 모노)
        f.write(pcm)
    os.chmod(os.path.join(PRINT_DIR, f"{ME}-{k}.pcm"), 0o600)
    if _enroll["left"] > 0:
        return "more", _enroll["left"]
    _save_print(_enroll["embs"]); _enroll["embs"] = []
    return "done", 0

def reenroll(name=ME):
    """voiceprint/ 의 녹음으로 지문을 다시 만든다. 쓴 녹음 수."""
    embs = []
    for f in sorted(os.listdir(PRINT_DIR)) if os.path.isdir(PRINT_DIR) else []:
        if f.startswith(name + "-") and f.endswith(".pcm"):
            _, s = speech(open(os.path.join(PRINT_DIR, f), "rb").read())
            e = embed(s)
            if e is not None:
                embs.append(e)
    if embs:
        _save_print(embs, name)
    return len(embs)

def forget(name=ME):
    selfdb.voice_forget(name)
    n = 0
    for f in os.listdir(PRINT_DIR) if os.path.isdir(PRINT_DIR) else []:
        if f.startswith(name + "-"):
            os.remove(os.path.join(PRINT_DIR, f)); n += 1
    return n

def report(days=7):
    """최근 클립의 말소리 길이와 닮음 점수: 버튼 클립(거의 주인)과 대화 모드 클립(주인 + 그 밖의 소리)을 나눠 본다."""
    rows = selfdb.db().execute("SELECT kind, ts, turn_json FROM episodes WHERE ts >= ? AND turn_json IS NOT NULL ORDER BY ts", (int(time.time() - days * 86400),)).fetchall()
    groups = {"버튼": [], "대화 모드": []}
    for r in rows:
        try:
            t = json.loads(r["turn_json"])
        except ValueError:
            continue
        if t.get("voice") is None:
            continue
        groups["대화 모드" if t.get("talk") else "버튼"].append((t["voice"], t.get("speech_s") or 0))
    out = []
    for name, g in groups.items():
        if not g:
            out.append(f"{name}: 점수 없음"); continue
        v = sorted(x for x, _ in g)
        q = lambda p: v[min(len(v) - 1, int(len(v) * p))]
        out.append(f"{name}: {len(g)}개 · 닮음 최저 {v[0]:.2f} / 10% {q(.1):.2f} / 가운데 {q(.5):.2f} / 90% {q(.9):.2f} / 최고 {v[-1]:.2f}"
                   f" · 2초 미만 {sum(1 for _, s in g if s < 2)}개")
    return "\n".join(out)

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "report":
        print(report(int(sys.argv[2]) if len(sys.argv) > 2 else 7))
    elif cmd == "wav":
        import wave
        w = wave.open(sys.argv[2]); pcm = w.readframes(w.getnframes())
        t0 = time.time(); s_s, s = speech(pcm); t1 = time.time(); sc = score(s); t2 = time.time()
        print(f"말소리 {s_s}초 / 클립 {len(pcm) / 2 / SR:.1f}초 (VAD {t1 - t0:.2f}s) · 닮음 {sc} (임베딩 {t2 - t1:.2f}s)")
    elif cmd == "reenroll":
        print(f"지문을 다시 만들었다: 녹음 {reenroll()}개")
    elif cmd == "forget":
        print(f"지웠다: 지문과 녹음 {forget()}개")
# 서버 설치 (한 번):
#   ~/venv/bin/pip install sherpa-onnx
#   cd voice/models && B=https://github.com/k2-fsa/sherpa-onnx/releases/download
#   curl -LO $B/asr-models/silero_vad.onnx
#   curl -LO $B/speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx      (주소의 recongition 은 저쪽 오타 그대로다)
