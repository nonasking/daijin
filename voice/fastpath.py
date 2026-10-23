#!/usr/bin/env python3
# 명령 지름길 — 짧고 분명한 명령은 모델 없이 규칙으로 알아듣고 바로 실행한다.
# 어떤 생각 엔진을 쓰든 같은 결과가 나오고(작은 모델이 "안 하고 했다"고 말할 틈이 없다), 1~2초로 빠르다.
# 애매하면 None 을 돌려주고 모델에 넘긴다: 물음, 부정("돌리지 마"), 긴 문장, 노드가 여럿 섞인 말.
#
# 입력은 normalize_nodes 를 거친 글 (레드/블루 → "1번"/"2번").
import re
from nodes import NODES, by_number, spoken, mentioned

MAX_LEN = 30
QUESTION = re.compile(r"[?？]|어때|뭐|왜|언제|어디|할까|해도 돼|돼\?|있어\?|켜져 있|꺼져 있|했어\?|줄래\?")
NEGATION = re.compile(r"지 ?마|말고|하지 말|안 돼|안돼|그만")
ALL = re.compile(r"전부|전체|모두|다 |둘 ?다|모든")
NODE_NUM = re.compile(r"(?<![0-9])([0-9])번")

COLORS = {"빨간": "red", "빨강": "red", "파란": "blue", "파랑": "blue", "초록": "green", "녹색": "green",
          "노란": "yellow", "노랑": "yellow", "하얀": "white", "흰": "white", "하늘": "cyan", "보라": "magenta", "분홍": "magenta"}
LIGHT = re.compile(r"불|led|LED|라이트|조명|빛")
OFF = re.compile(r"꺼|끄")
ON = re.compile(r"켜")
MOTOR = re.compile(r"모터.*(돌려|돌리|움직)|돌려 ?줘|돌려$")
BUZZ = re.compile(r"부저|삐|소리 ?내|알람")

def _node(text):
    """말에 나온 노드. 번호("1번")와 이름(레드, red: "레드랑"처럼 조사가 붙어 번호로 안 바뀐 것)을 함께 센다."""
    found = {by_number(int(n)) for n in NODE_NUM.findall(text)} | mentioned(text, numbers=False)
    found.discard(None)                                   # 표에 없는 번호: 아래에서 애매함으로 친다
    if len(found) > 1 or (NODE_NUM.search(text) and not found):
        return "ambiguous"
    if found:
        return found.pop()
    if ALL.search(text):
        return "all"
    return None

def parse(text):
    """(노드 id 또는 'all', 명령) 또는 None (모델에 넘길 것)."""
    t = (text or "").strip()
    if not t or len(t) > MAX_LEN or QUESTION.search(t) or NEGATION.search(t):
        return None
    node = _node(t)
    if node == "ambiguous":
        return None
    phrases = (NODES.get(node, {}).get("phrases", {}) if node not in (None, "all") else {})
    for word, cmd in phrases.items():                       # nodes.json 의 노드별 말 (예: 블루 "간식" → motor)
        if word in t:
            return node, cmd
    if LIGHT.search(t):
        color = next((c for w, c in COLORS.items() if w in t), None)
        cmd = "led:off" if OFF.search(t) else f"led:{color}" if color else "led:green" if ON.search(t) else None
        return (node or "all", cmd) if cmd else None
    if node is None:
        return None                                        # 불 말고는 어느 노드인지 분명해야 실행한다
    if MOTOR.search(t):
        return node, "motor"
    if BUZZ.search(t):
        return node, "buzz:alarm" if "알람" in t else "buzz:ok"
    return None

def _topic(nid):
    name = spoken(nid)
    c = name[-1]
    batchim = "가" <= c <= "힣" and (ord(c) - 0xAC00) % 28 != 0
    return name + ("은" if batchim else "는")

def reply_for(node, cmd, ok, out=""):
    """실행 결과 → 말할 한 문장. 실제 결과에 근거한 말만 한다."""
    who = "다들" if node == "all" else spoken(node)
    if not ok:
        if "오프라인" in out:
            return f"{_topic(node)} 지금 꺼져 있어서 못 했어." if node != "all" else "지금 노드에 닿지 않아서 못 했어."
        if "없는 명령" in out or "err:unknown" in out:
            return f"{_topic(node)} 그 기능이 없어."
        return f"{who}한테 보냈는데 대답이 없어." if node != "all" else "보냈는데 대답이 없어."
    if cmd == "motor":
        return f"{who} 모터 돌렸어."
    if cmd == "led:off":
        return f"{who} 불 껐어."
    if cmd.startswith("led:"):
        return f"{who} 불 켰어."
    if cmd.startswith("buzz"):
        return f"{who} 소리 냈어."
    return "보냈어."


# ---------- 시계로 답할 수 있는 물음 ----------
# 작은 모델은 날짜를 넘겨줘도 요일을 틀린다 (2026-10-11 실측 3번 중 1번). 시계가 정확히 아는 것은 시계가 답한다.
WEEKDAY = "월화수목금토일"
ASK_TIME = re.compile(r"(지금|현재)?\s*몇 ?시")
ASK_WEEKDAY = re.compile(r"무슨 ?요일|몇 ?요일|요일이")
ASK_DATE = re.compile(r"(오늘|지금).{0,4}(며칠|몇 ?일|날짜)|며칠이야|날짜가")
OTHER_DAY = re.compile(r"내일|모레|어제|그저께|다음 ?주|지난 ?주|생일|약속|몇 ?시에|몇 ?시까지|몇 ?시간")

def now_line(now=None):
    """로컬 모델에게 줄 지금: "2026년 10월 20일 화요일 14:05". 모델이 요일을 스스로 셈하지 않게 시계가 적어 준다."""
    import time
    lt = time.localtime(time.time() if now is None else now)
    return time.strftime("%Y년 %m월 %d일 ", lt) + WEEKDAY[lt.tm_wday] + "요일 " + time.strftime("%H:%M", lt)

def answer_clock(text, now=None):
    """시각·날짜·요일 물음이면 답 문장, 아니면 None. 다른 날이나 일정이 섞인 물음은 모델에 맡긴다."""
    import time
    t = (text or "").strip()
    if not t or len(t) > MAX_LEN or OTHER_DAY.search(t):
        return None
    lt = time.localtime(time.time() if now is None else now)
    ampm = "오전" if lt.tm_hour < 12 else "오후"
    h12 = lt.tm_hour % 12 or 12
    clock = f"{ampm} {h12}시" + (f" {lt.tm_min}분" if lt.tm_min else "")
    day = f"{lt.tm_mon}월 {lt.tm_mday}일 {WEEKDAY[lt.tm_wday]}요일"
    if ASK_TIME.search(t):
        return f"지금 {clock}" + ("이야." if clock.endswith("분") else "야.")
    if ASK_DATE.search(t):
        return f"오늘은 {day}이야."
    if ASK_WEEKDAY.search(t):
        return f"오늘은 {WEEKDAY[lt.tm_wday]}요일이야."   # "요일" 은 받침이 있어 "이야"
    return None
