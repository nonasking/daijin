#!/usr/bin/env python3
# 거절 장부의 규칙: 응답을 어떻게 분류하고, 분류가 정책을 어떻게 바꾸는지. 순수 함수만, DB 와 네트워크 없음.
# 근거는 docs/design-self-layer.md §5·§6: 무응답과 맥락 있는 거절은 다른 칸이고(CHI '26),
# 복구는 설명과 약속만 쓰며 부인은 없다(Esterwood & Robert 2021), 반복된 복구는 힘을 잃는다.
import json, re, time

RESPONSE_WINDOW_S = 600          # 먼저 말 건 뒤 응답을 기다리는 창
MAX_UNANSWERED_BACKOFF = 2       # presence 를 모르는 동안 무응답은 약한 거절: 레벨 2 까지만 올린다
MAX_REPAIRS_IN_A_ROW = 2         # 같은 추동에서 복구를 이만큼 연속한 뒤에도 거절이면 사람이 풀 때까지 끈다
MUTE_AFTER_NEGATIVE_S = 24 * 3600
FAR_FUTURE = 4_102_444_800       # 2100-01-01: "사람이 풀기 전까지"

# 분류 키워드. 음성 전사라 띄어쓰기·어미가 흔들리므로 어간 위주로 둔다. 순서가 우선순위다 (negative 가 refused 보다 먼저).
NEGATIVE = ("그만", "시끄러", "조용히 해", "조용", "닥쳐", "살살", "짜증", "왜 자꾸", "귀찮", "하지 마", "하지마", "말 걸지", "부르지 마")
REFUSED  = ("바빠", "바쁘", "나중에", "지금은", "이따", "회의", "자야", "잘게", "잘 게", "피곤", "운전", "통화", "다음에", "있다가")
ACK      = ("응", "어", "그래", "알았어", "알겠어", "오케이", "ok", "네", "고마워", "고맙", "괜찮아", "됐어", "좋아")

def classify(text):
    """응답 창 안에 들어온 말 → engaged | acknowledged | refused_context | negative.
    (창 안에 아무 말도 없는 경우의 unanswered 는 호출하는 쪽이 정한다.)"""
    t = (text or "").strip()
    if not t:
        return "unanswered"
    low = t.lower()
    if any(k in low for k in NEGATIVE):
        return "negative"
    if any(k in low for k in REFUSED):
        return "refused_context"
    body = re.sub(r"[\s.,!?~]", "", low)
    if len(body) <= 6 and any(body.startswith(k) or body == k for k in ACK):
        return "acknowledged"
    return "engaged"

def parse_quiet(spec):
    """'23-08' → (23, 8). 비거나 잘못되면 None (조용한 시간 없음)."""
    m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", spec or "")
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    return (a, b) if 0 <= a <= 23 and 0 <= b <= 23 else None

def in_quiet(hour, quiet):
    if not quiet:
        return False
    a, b = quiet
    return (a <= hour < b) if a < b else (hour >= a or hour < b)   # 23-08 처럼 자정을 넘는 구간

def next_allowed_ts(policy):
    """이 추동으로 다시 말을 걸 수 있는 가장 이른 시각. policy 는 selfdb.policy 의 한 줄(또는 같은 키의 dict)."""
    last = policy["last_ask_ts"] or 0
    wait = policy["base_interval_s"] * (2 ** (policy["backoff_level"] or 0))
    return max(last + wait, policy["muted_until"] or 0)

def avoid_hours(policy):
    try:
        return set(json.loads(policy["avoid_hours"] or "[]"))
    except (ValueError, TypeError):
        return set()

def apply_outcome(policy, response_class, asked_hour, repairs_in_a_row=0, now=None):
    """응답 분류 → 정책 변화. 바뀐 필드만 dict 로 돌려준다 (selfdb.policy_update 에 그대로 넘긴다)."""
    now = time.time() if now is None else now
    level = policy["backoff_level"] or 0
    change = {}
    if response_class == "engaged":
        change["backoff_level"] = 0
    elif response_class == "acknowledged":
        pass
    elif response_class == "refused_context":
        change["backoff_level"] = level + 1
        change["avoid_hours"] = sorted(avoid_hours(policy) | {f"{asked_hour:02d}"})
    elif response_class == "negative":
        change["backoff_level"] = level + 1
        if repairs_in_a_row >= MAX_REPAIRS_IN_A_ROW:
            change["muted_until"] = FAR_FUTURE          # 사람이 풀 때까지
        else:
            change["muted_until"] = int(now + MUTE_AFTER_NEGATIVE_S)
    elif response_class == "unanswered":
        change["backoff_level"] = min(level + 1, MAX_UNANSWERED_BACKOFF)
    return change

def needs_repair(last_class):
    return last_class in ("negative", "refused_context")

def when_word(asked_ts, now):
    """지난번 말을 건 때를 가리키는 말: 오늘이면 "아까는", 어제면 "어제는", 그 전이면 "지난번엔"."""
    days = (time.localtime(now).tm_yday - time.localtime(asked_ts).tm_yday) % 366 if now - asked_ts < 7 * 86400 else 7
    return "아까는" if days == 0 else "어제는" if days == 1 else "지난번엔"

def repair_line(strategy, trigger=None, promise=None, asked_ts=None, now=None):
    """복구 한 문장. explain 은 지난번에 왜 말 걸었는지, promise 는 무엇이 바뀌는지. 부인은 없다.
    trigger 는 "…해서" 꼴 (예: "14시간째 얘기를 못 해서"). 2026-10-12 실제로 "못 해서라서"가 나가서 이음을 고쳤다."""
    if strategy == "explain":
        when = when_word(asked_ts, time.time() if now is None else now) if asked_ts else "아까는"
        if not trigger:
            return f"{when} 걱정돼서 말 걸었어."
        reason = trigger if trigger.endswith("서") else trigger + "라서"
        return f"{when} {reason} 말 걸었어."
    if strategy == "promise":
        return f"앞으로는 {promise}." if promise else "앞으로는 덜 물을게."
    raise ValueError(strategy)

def promise_for(change):
    """정책 변화를 말로. 약속은 실제로 바뀐 것만 말한다."""
    if change.get("muted_until") == FAR_FUTURE:
        return "네가 풀어 줄 때까지 이 얘기는 안 꺼낼게"
    if change.get("muted_until"):
        return "하루는 이 얘기 안 할게"
    if change.get("avoid_hours"):
        return "이 시간대엔 안 물을게"
    if change.get("backoff_level"):
        return "더 뜸하게 물을게"
    return None
