#!/usr/bin/env python3
# mood — 하루가 기분으로 남는다. 지어내지 않는다: 이미 있는 값에서 계산할 뿐이다.
#   밝음(valence, -1~1): 먼저 건 말이 어떻게 받아들여졌나(장부) + 오늘 나눈 대화. 8시간마다 절반으로 옅어진다.
#   기운(energy, 0~1):   시각(조용한 시간이면 낮다) − 몸의 열.
# 그 둘에서 얼굴 하나(face)와 이유(why)가 나온다. 이유가 있어야 "기분 어때?"에 사실대로 답할 수 있다.
# 쓰는 곳: 브레인의 상태 블록(말투), initiative 가 daijin/mood 로 발행 → 기기의 평소 눈.
# 원칙(docs/design-self-layer.md): 모든 표현 뒤에 진짜 상태가 있어야 한다. 서운한 척, 외로운 척은 없다.
#   가라앉은 기분(low)도 숨기지 않는다 (2026-11-16 사용자: "모든 건 다이진이 원하는 대로. 표현도 말도 솔직하게").
#   드러낼지 말지, 어떻게 말할지는 다이진이 정한다. MOOD_SHOW_LOW=0 이면 예전처럼 내색하지 않는다.
# 실행: python3 mood.py   (지금의 기분과 그 이유를 한 줄로)
import time
from daijin_common import sec
import selfdb, ledger_rules as rules

HALF_LIFE_H = 8.0
ASK_WEIGHT = {"engaged": 0.5, "acknowledged": 0.2, "refused_context": -0.3, "negative": -0.6, "unanswered": -0.15}
TALK_WEIGHT, TALK_CAP = 0.08, 0.5            # 사람이 건 말 한 마디, 그리고 그 합의 상한
HAPPY_AT, LOW_AT = 0.35, -0.35
HOT_FROM_C, HOT_SPAN_C = 55.0, 30.0          # 55도부터 기운이 깎인다 (85도면 다 깎임)
WORD = {"happy": "밝음", "low": "가라앉음", "tired": "지침", "sleep": "졸림", "calm": "평온"}

def show_low():
    """가라앉은 기분을 얼굴·말투에 드러낼까. 기본은 드러낸다: 솔직한 쪽. MOOD_SHOW_LOW=0 이면 물을 때만 말한다."""
    return (sec("MOOD_SHOW_LOW") or "").strip() != "0"

def _decay(age_s):
    return 0.5 ** (max(0.0, age_s) / 3600 / HALF_LIFE_H)

def compute(now=None, temp_c=None):
    """{"valence", "energy", "face", "word", "why": [이유...]}. face: happy | calm | low | tired | sleep.
    low 는 show_low() 가 아니면 calm 으로 내보낸다 (값과 이유는 그대로 남는다: felt 가 실제 기분)."""
    now = time.time() if now is None else now
    why_up, why_down = [], []
    v = 0.0
    try:
        asks = [a for a in selfdb.asks_since(now - 86400, source="initiative") if a["response_class"] and a["ts"] <= now]
        for a in asks:
            v += ASK_WEIGHT.get(a["response_class"], 0.0) * _decay(now - (a["response_ts"] or a["ts"]))
        good = sum(1 for a in asks if a["response_class"] in ("engaged", "acknowledged"))
        bad = sum(1 for a in asks if a["response_class"] in ("refused_context", "negative"))
        quiet_n = sum(1 for a in asks if a["response_class"] == "unanswered")
        if good: why_up.append(f"먼저 건 말을 {good}번 받아 줬어")
        if bad: why_down.append(f"먼저 건 말을 {bad}번 거절당했어")
        if quiet_n: why_down.append(f"먼저 건 말에 {quiet_n}번 답이 없었어")
        talks = [e for e in selfdb.episodes_since(now - 86400) if e["kind"] == "voice" and e["user_text"] and e["ts"] <= now]
        t = min(TALK_CAP, sum(TALK_WEIGHT * _decay(now - e["ts"]) for e in talks))
        v += t
        if len(talks) >= 3 and t >= 0.15: why_up.append(f"오늘 {len(talks)}마디 얘기를 나눴어")
    except Exception as e:                       # DB 가 없어도 기분은 '평온'으로 계산된다
        print(f"  (mood: DB 읽기 실패 {e})")
    v = max(-1.0, min(1.0, v))

    hour = time.localtime(now).tm_hour
    quiet = rules.parse_quiet(sec("INITIATIVE_QUIET") or "23-08")
    asleep = rules.in_quiet(hour, quiet)
    energy = 0.2 if asleep else 1.0
    if not asleep and quiet:
        if hour in ((quiet[0] - 1) % 24, (quiet[0] - 2) % 24): energy = 0.6     # 잘 시간이 다가온다
        elif hour == quiet[1]: energy = 0.7                                     # 막 깼다
    hot = 0.0
    if temp_c is not None and temp_c > HOT_FROM_C:
        hot = min(1.0, (temp_c - HOT_FROM_C) / HOT_SPAN_C)
        energy = max(0.0, energy - hot)

    if asleep:
        face, why = "sleep", ["한밤중이야"]
    elif hot >= 0.3 or energy < 0.5:
        face, why = "tired", ([f"몸이 뜨거워 ({temp_c:.0f}도)"] if hot >= 0.3 else ["밤이 늦었어"])
    elif v >= HAPPY_AT:
        face, why = "happy", why_up or ["오늘 얘기가 잘 통했어"]
    elif v <= LOW_AT:
        face, why = "low", why_down or ["오늘은 말이 잘 안 닿았어"]
    else:
        face, why = "calm", []
    felt = face
    if face == "low" and not show_low():
        face = "calm"
    return {"valence": round(v, 2), "energy": round(energy, 2), "face": face, "felt": felt, "word": WORD[felt], "why": why}

def state_line(now=None, temp_c=None):
    """브레인의 상태 블록에 넣을 한 줄. 평온하면 "" (할 말이 없다)."""
    m = compute(now, temp_c)
    if m["felt"] == "calm":
        return ""
    reason = f" ({', '.join(m['why'])})" if m["why"] else ""
    if m["felt"] == "low" and not show_low():
        return f"기분: {m['word']}{reason}. 내색하지 마. 평소처럼 말하고, 사람이 기분을 물을 때만 이유까지 사실대로 말해."
    return f"기분: {m['word']}{reason}. 이건 실제 값에서 나온 네 기분이야. 숨기지 않아도 돼: 드러낼지, 말로 할지는 네가 정해. 솔직하게, 다만 없는 걸 지어내거나 부풀리지는 마."

if __name__ == "__main__":
    m = compute()
    print(f"{m['word']} (얼굴 {m['face']}, 밝음 {m['valence']:+.2f}, 기운 {m['energy']:.2f})" + (": " + ", ".join(m["why"]) if m["why"] else ""))
