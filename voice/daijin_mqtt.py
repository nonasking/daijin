#!/usr/bin/env python3
# daijin 브레인 (MQTT 클라이언트) — 브레인 머신(맥 또는 리눅스 서버)이 HiveMQ에 outbound 접속해 오디오 토픽으로 대화.
#   구독 daijin/audio/in  (디바이스가 녹음한 ADPCM 청크) → STT(Groq Whisper API 또는 로컬 whisper.cpp) → Claude → TTS
#   발행 daijin/audio/out (답 ADPCM 청크, 문장 단위 스트리밍)  + daijin/text/in, daijin/text/out (디버그)
#   프로토콜: docs/chunking-protocol.md. 디바이스 없이 왕복 시험: voice/test_device.py
# 실행:  python3 -u voice/daijin_mqtt.py
#   상시 구동: 맥은 LaunchAgent com.daijin.brain (voice/install-agents.sh), 리눅스는 systemd 사용자 서비스 (voice/install-services.sh)
# 이식성: 실행 파일은 PATH에서 찾고 macOS 전용 도구(afconvert, say)는 맥에서만 쓴다. 리눅스 의존은 ffmpeg 하나.
#
# 설계 노트:
# - 보안: 도구 전면 개방(사용자 결정 2026-07-04) — 브로커 자격증명이 곧 보안 경계.
#   상세는 아래 ALLOWED_TOOLS 주석 참조.
# - 대화 연속성: session_id를 파일(.daijin_session)에 영속 → 재시작해도 이어지고,
#   같은 디렉토리의 다른 claude 세션과 절대 섞이지 않음 (--continue 사용 금지).
# - 서비스 내성: 초기 브로커 연결 실패 시 재시도 루프, 런타임 끊김은 paho 자동 재접속 (daijin_common).

import subprocess, re, os, sys, time, json, threading, queue, shutil, socket, uuid, types, pathlib
import array, asyncio, math, tempfile
import urllib.request, urllib.parse
try:
    import edge_tts                    # pip install edge-tts. 없으면 edge 엔진만 못 쓰고 다음 엔진으로 넘어간다
except ImportError:
    edge_tts = None
try:
    import supertonic                  # pip install supertonic. 온디바이스 한국어 TTS (ONNX, CPU). 없으면 그 엔진만 빠진다
except ImportError:
    supertonic = None
from daijin_common import VOICE, REPO, T_AUDIO_IN, sec, load_hooks, cloud_client, connect_with_retry, broker
from nodes import NODES, spoken, spellings
import selfdb
import local_mind, fastpath

HOME    = os.path.expanduser("~")
# 헬퍼 스크립트(dev.sh 등)가 쓸 파이썬 = 브레인을 띄운 이 파이썬 (paho 가 깔려 있는 것). 에이전트의 셸과 local_hooks 가 물려받는다.
os.environ.setdefault("DAIJIN_PYTHON", sys.executable)
# 경로(VOICE, REPO)는 daijin_common 이 이 저장소의 위치에서 유도한다. 저장소를 어디에 클론해도 된다.
IS_MAC  = sys.platform == "darwin"
def _which(name, *candidates):
    """실행 파일 찾기: 알려진 절대 경로 후보 → PATH → 이름 그대로(없으면 실행 시점에 실패로 드러남)."""
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return shutil.which(name) or name
WHISPER = os.environ.get("WHISPER_CLI") or _which("whisper-cli", "/opt/homebrew/bin/whisper-cli")
MODEL   = f"{VOICE}/models/ggml-large-v3-turbo-q5_0.bin"
CLAUDE  = _which("claude", f"{HOME}/.local/bin/claude")
FFMPEG  = _which("ffmpeg", "/opt/homebrew/bin/ffmpeg")
AFCONVERT = "/usr/bin/afconvert"       # macOS 내장 — ffmpeg가 없을 때만
SESSION_FILE = f"{VOICE}/.daijin_session"
TTS_VOICE = "Yuna"                     # 최종 폴백 (오프라인 보장)
ELEVEN_VOICE = "TFUX5RKA9yUMr26dSJqF"  # daijin 보이스 (Voice Design으로 생성, 크레딧제)
EDGE_VOICE = "de-DE-SeraphinaMultilingualNeural"  # 무료·무제한 기본 보이스 (사용자 선택 2026-07-05)

T_AUDIO_OUT = "daijin/audio/out"
T_TXT_IN    = "daijin/text/in"
T_TXT_OUT   = "daijin/text/out"
# 능동 발화 — 다이진이 먼저 말하는 경로 (음성 입력 없이 텍스트로 발화 트리거)
T_SAY       = "daijin/say"    # 페이로드 텍스트를 그대로 TTS → 스피커 (알림·차임용)
T_ASK       = "daijin/ask"    # 페이로드를 에이전트에 전달 → 스스로 표현해 말함 (보고용)
# 브레인 자신의 상태 (디바이스·노드의 status 와 같은 꼴). 꺼지면 last will 이 offline 으로 덮는다.
T_BRAIN_STATUS = "daijin/brain/status"   # retained JSON: online, ts, up_s, turns, model, host
T_BRAIN_TURN   = "daijin/brain/turn"     # 턴마다 JSON 한 줄: 단계별 소요 시간 (계측, retained 아님)
T_BRAIN_ALERT  = "daijin/brain/alert"    # retained JSON: 사람이 봐야 할 문제 (인증 만료 등). 해결되면 빈 페이로드로 지운다

# 허용 도구 — 사용자 결정(2026-07-04)으로 전면 개방. 보안 노트: 브로커 자격증명이
# 곧 브레인 머신의 명령 입력 통로가 되므로, HiveMQ 비밀번호 관리가 곧 보안 경계다.
ALLOWED_TOOLS = "Bash,WebSearch,WebFetch,Read,Glob,Grep,Write,Edit"
# 속도: 모델은 secrets 의 BRAIN_MODEL (기본 claude-sonnet-5-5), 추론 강도는 BRAIN_EFFORT (기본 low: 대화·명령 판단엔 충분하고
# 첫 토큰이 가장 빠르다. 2026-10-08 맥 실측, 한 글자 답: sonnet-5-5 low 2.6초, haiku-5-5 low 1.7초, sonnet-5 7.7초).
# 한 턴의 도구 호출 상한은 BRAIN_MAX_TURNS (기본 12: 플릿 조회 + 노드 명령 몇 개면 4~6턴). 폭주하면 여기서 끊긴다.
# MCP(Gmail 등)는 매 턴 접속 비용만 들어서 끈다 (--strict-mcp-config + 빈 설정).
# 주의: --bare 는 쓰지 않는다. 구독 토큰(CLAUDE_CODE_OAUTH_TOKEN)을 읽지 않아 서버에서 인증이 깨진다.
DEFAULT_MODEL = "claude-sonnet-5-5"
def _speed_opts():
    return ["--model", sec("BRAIN_MODEL") or DEFAULT_MODEL,
            "--effort", sec("BRAIN_EFFORT") or "low",
            "--max-turns", sec("BRAIN_MAX_TURNS") or "12",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']

def _props():
    """각 노드에 물려 있는 물리 소품 설명. 있으면 시스템 프롬프트에 붙인다.
    voice/props.local.txt(저장소에 넣지 않는 이 설치본의 것)가 있으면 그것을, 없으면 voice/props.txt(예시)를 읽는다."""
    for name in ("props.local.txt", "props.txt"):
        try:
            t = pathlib.Path(VOICE, name).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            continue
        return (" 지금 홈 노드들에 붙어 있는 실제 소품과 할 수 있는 장난: " + t.replace("\n", " ") + " ") if t else ""
    return ""

# 노드 소개 문구는 voice/nodes.json 에서 만든다 (예: "레드=dev1, 블루=dev2")
_ns = sorted(NODES.items(), key=lambda kv: kv[1]["n"])
_NODE_INTRO       = ", ".join(f'{d["name"]}={nid}' for nid, d in _ns)
_NODE_NAMES_DOT   = "·".join(d["name"] for _, d in _ns)
_NODE_NAMES_COMMA = ", ".join(d["name"] for _, d in _ns)
_NODE_NUMS_DOT    = "·".join(f'{d["n"]}번' for _, d in _ns)
_NODE_FORMS       = ", ".join([str(_ns[0][1]["n"]), f'{_ns[0][1]["n"]}번'] + _ns[0][1].get("aliases", [])[:1] + [_ns[0][1]["name"]])

SYS = ("너는 'daijin'이라는 이름의 AI 음성 대화 친구야. 따뜻하고 친근하게 한국어로 "
       "2~3문장 이내로 짧게 답해. 이모지·마크다운·특수기호는 쓰지 마(음성으로 읽힘). "
       "너는 이 컴퓨터(네 브레인이 돌아가는 서버)에서 도구를 자유롭게 쓸 수 있어 — 웹 검색, 파일 읽고 쓰기, 셸 명령 실행. "
       "부탁받으면 직접 실행하고 결과를 짧게 요약해서 말해줘. "
       f"집 LED(각 노드의 온보드 RGB)는 'bash {VOICE}/dev.sh all led:<색>' (색: red green blue yellow cyan magenta white off; "
       "꺼=off, 켜=green). "
       f"집안 홈 노드 제어는 'bash {VOICE}/dev.sh <노드|all> <명령>' (노드는 {_NODE_INTRO}. 사용자는 {_NODE_NAMES_DOT}라고 부르고 텍스트로는 {_NODE_NUMS_DOT}으로 들어올 수 있어, 같은 뜻이야. dev.sh는 {_NODE_FORMS} 다 받아. 말할 때는 {_NODE_NAMES_COMMA}라고 불러) "
       "(명령: led:색 · servo:0~180 · poke:각도[:ms]=갔다가 원위치(스위치 누르기·풍선 터뜨리기) · "
       "relay:on/off/pulse[:ms] · fan:on/off · step:±각도(스텝모터, 느림) · motor[:각도]=노드 종류 상관없이 모터 돌리기(서보면 갔다 옴, 스텝이면 회전; 모터 돌려달라면 이걸 써) · buzz:alarm/ok/fail/ms · ping · caps. "
       "ack까지 확인하되 말할 때는 ack라는 단어를 쓰지 말고 보냈어/갔어라고만 해. err:unknown이면 그 노드에 그 기능이 없는 거야). "
       f"홈 노드들의 상태·온라인 여부 조회는 'bash {VOICE}/fleet.sh' "
       "(OFFLINE=전원/네트워크 끊김, STALE=하트비트 끊김 — 발견하면 원인 추측과 함께 알려줘). "
       "네 몸(디바이스) 상태 — 온도, WiFi, 켜진 시간 — 를 물으면 "
       f"'bash {VOICE}/status.sh' 를 실행해서 JSON을 읽고 자연스럽게 말해줘 "
       "(temp_c=칩 온도이니 몸 온도처럼, rssi=WiFi 신호세기(-50 좋음, -80 나쁨), "
       "age_s=그 값이 몇 초 전 것인지: 150을 넘으면 지금 값이 아니니 그렇게 말해. FAIL이 나오면 디바이스가 꺼진 거야). "
       "단, 되돌리기 어렵거나 파괴적인 작업(파일 삭제·이동, 외부로 전송, 설치/제거)은 "
       "실행하기 전에 반드시 말로 확인을 받아. 도구 출력은 그대로 읽지 말고 핵심만 말해."
       + _props())
EMOJI = re.compile(r"[\U0001F000-\U0001FAFF☀-➿←-⇿*#`_]")

# ---------- 지금 상태 블록 ----------
# 브레인이 디바이스·노드 상태 토픽을 구독해 마지막 값을 들고 있다가, 턴마다 시스템 프롬프트 끝에 붙인다.
# 모델이 물어볼 필요 없는 것(몸, 노드, 먼저 말 건 이력, 기억, 인격)을 미리 주는 것: 도구 호출이 줄고 지어낼 틈이 없어진다.
# 값이 오래됐으면 오래됐다고 같이 적는다. 설계는 docs/design-self-layer.md §7.
T_DEV_STATUS  = "daijin/status"
T_NODE_STATUS = "daijin/dev/+/status"
_body = {"device": None, "device_ts": 0, "nodes": {}}     # nodes: id -> (json dict, 수신 시각)
STATE_MAX_CHARS = 600

def note_status(topic, payload):
    try:
        d = json.loads(payload)
    except (ValueError, TypeError):
        return
    if topic == T_DEV_STATUS:
        _body["device"], _body["device_ts"] = d, time.time()
    elif topic.startswith("daijin/dev/"):
        _body["nodes"][topic.split("/")[2]] = (d, time.time())

def _age(sec_):
    return f"{int(sec_)}초 전" if sec_ < 120 else f"{int(sec_ // 60)}분 전" if sec_ < 7200 else f"{int(sec_ // 3600)}시간 전"

def state_block(now=None):
    now = time.time() if now is None else now
    parts = []
    d = _body["device"]
    if d:
        if d.get("online") is False:
            parts.append("몸(디바이스): 꺼져 있음")
        else:
            age = now - (d.get("ts") or _body["device_ts"])
            parts.append(f"몸: 온도 {d.get('temp_c', '?')}도, 신호 {d.get('rssi', '?')}, 켜진 지 {int((d.get('uptime_s') or 0) // 60)}분"
                         + (f" ({_age(age)} 값)" if age > 150 else ""))
    nodes = []
    excused = selfdb.node_excused()
    for nid, (nd, rts) in sorted(_body["nodes"].items()):
        name = spoken(nid)
        if nd.get("online"):
            beat = nd.get("ts") or 0
            stale = beat > 1_700_000_000 and now - beat > 90
            nodes.append(f"{name} 켜짐" + (" (하트비트 끊김)" if stale else ""))
        else:
            nodes.append(f"{name} 꺼짐" + (" (사람이 일부러 꺼 둠, 걱정하지 마)" if nid in excused else ""))
    if nodes:
        parts.append("노드: " + ", ".join(nodes))
    if token_line(now):
        parts.append(token_line(now))
    try:
        asks = selfdb.recent_asks(3)
        if asks:
            kor = {"engaged": "대화로 이어짐", "acknowledged": "짧게 답함", "refused_context": "사정이 있어 거절",
                   "negative": "싫어함", "unanswered": "응답 없음", None: "응답 기다리는 중"}
            parts.append("내가 먼저 말 건 기록: " + " / ".join(
                f"{_age(now - a['ts'])} '{a['text'][:24]}' → {kor.get(a['response_class'], a['response_class'])}" for a in asks))
        mems = selfdb.top_memories(5, 200)
        if mems:
            parts.append("기억: " + " · ".join(m["text"] for m in mems))
        cur = selfdb.persona_current()
        if cur:
            parts.append("나에 대해: " + cur["text"].replace("\n", " ")[:300])
    except Exception as e:
        print(f"  (상태 블록: DB 읽기 실패 {e})")
    if not parts:
        return ""
    text = " [지금 상태, 이미 최신이니 다시 조회하지 않아도 돼] " + " | ".join(parts)
    return text[:STATE_MAX_CHARS]

def sys_prompt():
    """시스템 프롬프트는 이제 고정이다. 턴마다 바뀌는 상태 블록은 with_state() 로 사람의 말 뒤에 붙는다:
    상주 프로세스는 시스템 프롬프트를 시작할 때 한 번만 받으므로, 매 턴 바뀌는 것은 턴 안에 있어야 한다."""
    return SYS

def with_state(text, now=None):
    """사람의 말 + 지금 상태 블록. 상태가 비어 있으면 말만."""
    s = state_block(now).strip()
    return f"{text}\n\n{s}" if s else text

# ---------- 그 사이 내가 먼저 한 말 ----------
# daijin/say 로 나간 말은 에이전트를 거치지 않아 고정 세션의 대화 이력에 없다. 그러면 모델은 상태 블록에 기록이 있어도
# 자기 이력을 더 믿고 "먼저 말 건 적 없어"라고 답한다 (2026-10-10 실측: 같은 질문에 새 세션은 맞게, 고정 세션의 복제는 틀리게 답함).
# 그래서 에이전트에 넘기는 말 앞에, 직전 에이전트 턴 이후 say 로 나간 말을 붙인다. 이 줄이 세션 이력에 남으므로 이후로도 기억된다.
PROACTIVE_MAX = 5
PROACTIVE_MAX_CHARS = 300

def with_proactive_context(text, now=None):
    now = time.time() if now is None else now
    try:
        eps = [e for e in selfdb.episodes_since(now - 86400) if e["ts"] <= now]
    except Exception as e:
        print(f"  (그 사이 한 말: DB 읽기 실패 {e})")
        return text
    last_agent = max((e["ts"] for e in eps if e["kind"] in ("voice", "ask") and e["reply"]), default=0)
    said = [e for e in eps if e["kind"] == "say" and e["reply"] and e["ts"] > last_agent][-PROACTIVE_MAX:]
    if not said:
        return text
    lines, used = [], 0
    for e in said:
        line = f"{time.strftime('%H:%M', time.localtime(e['ts']))} \"{e['reply']}\""
        if used + len(line) > PROACTIVE_MAX_CHARS:
            break
        lines.append(line); used += len(line)
    return f"(지난 대화 뒤에 네가 먼저 스피커로 한 말: {' / '.join(lines)}) " + text

def load_session():
    try:
        with open(SESSION_FILE) as f:
            s = f.read().strip()
        return s or None
    except FileNotFoundError:
        return None

def save_session(sid):
    if not sid:
        return
    old = load_session()
    if old and old != sid:
        # 갈아타기 직전의 세션 id를 남겨 둔다. 잘못 새 세션으로 넘어갔을 때
        # .daijin_session.prev 의 내용을 .daijin_session 에 되돌려 쓰면 기억이 돌아온다.
        with open(SESSION_FILE + ".prev", "w") as f:
            f.write(old)
    with open(SESSION_FILE, "w") as f:
        f.write(sid)

# ---------- 로컬 확장 (선택): voice/local_hooks.py, 인터페이스는 daijin_common.load_hooks 참고 ----------
HOOKS = load_hooks(announce=True)

def intercepted(client, text, kind, t0):
    fn = getattr(HOOKS, "intercept", None)
    if fn is None:
        return False
    ctx = types.SimpleNamespace(say=lambda line: speak_stream(client, iter([line]), t0),
                                sec=sec, voice_dir=VOICE, t0=t0)
    try:
        return bool(fn(text, kind, ctx))
    except Exception as e:
        print(f"  ⚠️ local_hooks.intercept 오류 (에이전트로 넘김): {e}")
        return False

# 어휘 바이어스 — 도메인 단어를 알려주면 오인식이 크게 줄어드는 것을 실측으로 확인
# (2026-08-16: 같은 녹음에서 "빨갛을 켜줘" → "빨간 불 켜 줘"로 교정됨)
# 주의: whisper는 프롬프트를 "직전 대화 전사"로 취급한다. 라벨식("장치 이름: ...")으로 쓰면
# 그 라벨이 전사 앞에 새어 들어옴(실측 2026-08-17) → 자연스러운 문장 나열로 쓸 것.
STT_PROMPT = (getattr(HOOKS, "STT_PROMPT_EXTRA", "") +
              "다이진, 레드 모터 돌려 줘. 블루 간식 줘. 레드랑 블루 순서대로 돌려 줘. "
              "레드는 서보, 블루는 스텝모터. 블루 먼저, 그 다음에 레드. 십 초 뒤에 돌려 줘. "
              "장치 상태 알려줘. 전부 꺼 줘. 응, 알았어.")

# STT가 노드 이름을 제각각 적는다("대부 1", "데브 1", "dev 1", "1번 장치") → 브레인에 넘기기 전에 "1번"으로 통일
# 예전 호칭 "일 번/이 번"과 dev1/dev2 표기도 호환용으로 받는다. STT는 이걸 "한 번", "두 번", "일번", "1 번" 등으로 적는다.
# 전부 내부 ID "1번"/"2번"으로 통일. "한 번 더" 같은 횟수 표현과 겹치는 게 위험이라, 뒤에 "더/만/씩/에"가 오거나
# "번 더/번 돌"처럼 횟수로 읽히는 꼴은 손대지 않는다.
# 번호와 이름은 voice/nodes.json 에서 온다: 노드를 늘리면 여기 정규식도 따라 늘어난다.
_KO_NUM = {1: ("일", "한"), 2: ("이", "두"), 3: ("삼", "세"), 4: ("사", "네"), 5: ("오", "다섯"), 6: ("육", "여섯")}
_NUMS   = sorted(d["n"] for d in NODES.values())
_DIGITS = "".join(str(n) for n in _NUMS)                              # "12"
_KO2N   = {w: n for n in _NUMS for w in _KO_NUM.get(n, ())}           # {"일":1, "한":1, "이":2, "두":2}
_NODE_ALIAS = re.compile(
    r"(?<![0-9])(?:"
    rf"(?:dev|데브|대브|대부|디브|devi)\s*([{_DIGITS}])"                                   # dev1, 대부 2
    rf"|(?:([{_DIGITS}])|({'|'.join(_KO2N)}))\s*번(?:\s*(?:장치|노드|기기))?"              # 1번, 일 번, 한 번, 이 번, 두 번
    r")(?![0-9])(?!\s*(?:더|만|씩|에|엔|째|은|는|처럼|만큼))", re.I)
def _alias_sub(m):
    n = m.group(1) or m.group(2) or str(_KO2N[m.group(3)])
    return n + "번"
# 노드 호칭은 "레드/블루" (red/blue). 2026-09-22 실측: "일 번/이 번"은 받침 하나 차이라 소음 10dB부터 원본 PCM에서도
# 뒤집혔고(4/16), 레드/블루는 같은 조건에서 0/16. 내부 ID는 그대로 1번/2번(dev1/dev2)이고 음성 입출력에서만 색 이름을 쓴다.
# 한국어 색 이름(빨강/파랑)은 LED 색 명령("빨간 불 켜 줘")과 겹쳐서 노드 이름으로 받지 않는다.
_NAME2N = {w.lower(): d["n"] for nid, d in NODES.items() for w in spellings(nid)}
_NODE_NAME = re.compile(r"(?<![가-힣A-Za-z])(" + "|".join(re.escape(w) for w in _NAME2N) + r")(?![가-힣A-Za-z])", re.I)
def _name_sub(m):
    return f"{_NAME2N[m.group(1).lower()]}번"
def normalize_nodes(t):
    t = _NODE_NAME.sub(_name_sub, t)
    return _NODE_ALIAS.sub(_alias_sub, t)

_PROMPT_SENTS = [x.strip() for x in re.split(r"[.!?]", normalize_nodes(STT_PROMPT)) if x.strip()]
# 전사 신뢰도(세그먼트 평균 로그확률)가 이보다 높으면 프롬프트 예문과 똑같은 말이라도 진짜로 한 말로 본다.
# 2026-10-02 실측(Groq large-v3-turbo): 예문과 같은 문장을 실제로 말한 음성은 -0.01 ~ -0.43, 잡음만 든 클립은 -0.99 ~ -1.08.
CONFIDENT_LOGPROB = -0.6

def _looks_hallucinated(t, logprob=None):
    """whisper는 무음·잡음에서 프롬프트 예문을 그대로 뱉는다. 같은 문장이 2회 이상 반복되거나
    전사가 프롬프트 예문 하나로만 이뤄져 있으면 환청으로 본다.
    다만 예문은 실제로 자주 하는 말이기도 하다("전부 꺼 줘"). STT 가 신뢰도를 알려 주고 그 값이 높으면
    예문과 같아도 버리지 않는다. 신뢰도를 모르면(로컬 whisper) 예전처럼 버린다."""
    sents = [x.strip() for x in re.split(r"[.!?]", t) if x.strip()]
    if len(sents) >= 2 and len(set(sents)) == 1:
        return True
    if len(sents) == 1 and sents[0] in _PROMPT_SENTS:
        return logprob is None or logprob < CONFIDENT_LOGPROB
    return False

# ---------- STT 백엔드 ----------
# secrets의 STT_ENGINE: groq = Groq Whisper API (지금 로컬로 쓰는 것과 같은 large-v3-turbo, 무료 티어면 충분)
#                        whisper = 로컬 whisper.cpp (맥 GPU에선 빠르지만 2vCPU VPS에선 클립당 수십 초)
# 비우면 GROQ_API_KEY가 있을 때 groq, 없으면 whisper. groq가 실패하면 로컬 모델이 있을 때 폴백.
GROQ_URL   = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3-turbo"
WHISPER_URL_DEFAULT = "http://127.0.0.1:18080/inference"   # whisper.cpp 의 whisper-server (voice/systemd/whisper-server.service)

def _stt_engine():
    """secrets 의 STT_ENGINE 이 비어 있으면: whisper_server 가 떠 있으면 그것, 아니면 Groq 키가 있을 때 groq, 아니면 로컬 whisper-cli."""
    e = sec("STT_ENGINE")
    if e:
        return e
    if _whisper_server_up():
        return "whisper_server"
    return "groq" if sec("GROQ_API_KEY") else "whisper"

def _whisper_server_up(timeout=1.5):
    """whisper 서버 포트가 열려 있나. 사설망 너머의 맥도 잡도록 1.5초 (꺼져 있으면 여기서 바로 판정해 Groq 로 넘긴다)."""
    url = sec("WHISPER_URL") or WHISPER_URL_DEFAULT
    try:
        host, port = urllib.parse.urlparse(url).hostname, urllib.parse.urlparse(url).port or 80
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False

def _multipart(fields, filename, filedata, content_type="audio/wav"):
    """multipart/form-data 본문 조립 (stdlib만으로 — requests 의존 없이). (body, Content-Type) 반환."""
    boundary = "----daijin" + uuid.uuid4().hex
    body = bytearray()
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode("utf-8")
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
             f"Content-Type: {content_type}\r\n\r\n").encode("utf-8") + filedata + b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")
    return bytes(body), f"multipart/form-data; boundary={boundary}"

def stt_http(url, wav, headers=None, model=None, timeout=20):
    """OpenAI 호환 전사 엔드포인트(Groq, whisper.cpp 서버) 공통. (전사, 신뢰도).
    신뢰도 = 세그먼트 avg_logprob 의 평균 (0 에 가까울수록 확실, 세그먼트가 없으면 None). prompt 는 로컬 whisper 와 같은
    어휘 바이어스 문장(레드/블루 오인식 교정). verbose_json 이라야 세그먼트가 온다 (whisper-server 도 지원, 2026-10-10 확인)."""
    fields = {"language": "ko", "prompt": STT_PROMPT, "response_format": "verbose_json"}
    if model:
        fields["model"] = model
    body, ctype = _multipart(fields, "clip.wav", open(wav, "rb").read())
    # User-Agent 필수: Groq 앞단 Cloudflare가 urllib 기본값(Python-urllib)을 403(1010)으로 막는다
    h = {"Content-Type": ctype, "User-Agent": "daijin-brain/1.0", **(headers or {})}
    req = urllib.request.Request(url, data=body, method="POST", headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        d = json.loads(resp.read())
    probs = [seg["avg_logprob"] for seg in d.get("segments") or [] if "avg_logprob" in seg]
    return (d.get("text") or "").strip(), (sum(probs) / len(probs) if probs else None)

def stt_groq(wav):
    return stt_http(GROQ_URL, wav, headers={"Authorization": f"Bearer {sec('GROQ_API_KEY')}"}, model=GROQ_MODEL)

def stt_whisper_server(wav):
    """로컬 whisper.cpp 서버. 맥 M1 Pro 실측 RTF 0.4 (verbose_json). 모델은 서버가 들고 있어 model 필드가 없다."""
    return stt_http(sec("WHISPER_URL") or WHISPER_URL_DEFAULT, wav, timeout=60)

def stt_whisper(wav):
    r = subprocess.run([WHISPER,"-m",MODEL,"-l","ko","-nt","-np",
                        "--prompt",STT_PROMPT,"-f",wav],
                       capture_output=True, text=True)
    return r.stdout.strip(), None            # whisper-cli 는 신뢰도를 내주지 않는다

def stt(wav):
    """(전사, 신뢰도 또는 None). 순서: 고른 엔진 → (whisper_server 였으면 groq, groq 였으면 닿는 whisper_server) → 로컬 whisper-cli."""
    engine = _stt_engine()
    if engine == "whisper_server" and not _whisper_server_up():
        print(f"  (whisper 서버에 닿지 않음 → {'Groq' if sec('GROQ_API_KEY') else '로컬 whisper'})")
        engine = "groq" if sec("GROQ_API_KEY") else "whisper"
    if engine == "whisper_server":
        try:
            return stt_whisper_server(wav)
        except Exception as e:
            print(f"  (whisper 서버 STT 실패: {e} → {'Groq' if sec('GROQ_API_KEY') else '로컬 whisper'} 폴백)")
            engine = "groq" if sec("GROQ_API_KEY") else "whisper"
    if engine == "groq":
        try:
            return stt_groq(wav)
        except Exception as e:
            alt = sec("WHISPER_URL") and _whisper_server_up()
            print(f"  (Groq STT 실패: {e} → {'whisper 서버' if alt else '로컬 whisper'} 폴백)")
            if alt:
                try:
                    return stt_whisper_server(wav)
                except Exception as e2:
                    print(f"  (whisper 서버 STT 도 실패: {e2} → 로컬 whisper)")
    if not os.path.exists(MODEL):
        print(f"  ⚠️ STT 불가: 로컬 모델 없음({MODEL}) — secrets에 GROQ_API_KEY를 넣거나 모델을 내려받아야 해")
        return "", None
    return stt_whisper(wav)

def _claude_env():
    """헤드리스 서버용 인증: secrets의 CLAUDE_CODE_OAUTH_TOKEN(`claude setup-token`으로 발급, 구독 1년 토큰)을
    claude 자식 프로세스 환경에 넣는다. 이미 로그인된 맥처럼 비워 두면 평소 인증 그대로."""
    env = os.environ.copy()
    tok = sec("CLAUDE_CODE_OAUTH_TOKEN")
    if tok and not env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    return env

# ---------- 문장 스트리밍: Claude 답을 문장 단위로 실시간 yield ----------
# 전체 답변 완성을 기다리지 않고 첫 문장부터 TTS·재생 → 체감 지연 대폭 감소
# 마침표는 뒤에 공백이 와야 문장 끝. "36.1도"처럼 숫자 사이 마침표에서 끊겨 "36." "1도야"로 말하던 버그(2026-09-17).
# 스트림 끝의 마지막 문장은 공백이 없어도 rest 플러시로 나간다.
_SENT_END = re.compile(r"[!?…]+[\"')\]]*\s*|\.+[\"')\]]*\s+")

# 에이전트 한 턴의 상한. 스텝모터 한 바퀴(6초)나 웹 검색이 낀 턴도 보통 1분 안쪽이다.
# 이걸 넘기면 멈춘 것으로 보고 죽인다 (안 그러면 워커가 영원히 막혀 이후의 모든 말이 큐에 쌓인다).
BRAIN_TIMEOUT_S = 180
# 세션 재개가 "그 세션이 없다"로 실패했을 때만 새 세션으로 넘어간다. 일시적 오류(네트워크, 사용량 한도, 과부하)에
# 새 세션을 만들면 세션 파일이 덮여 대화 기억이 통째로 사라진다. 그 밖의 오류가 연달아 나면 그때 포기하고 넘어간다.
_SESSION_GONE = "No conversation found"
RESUME_GIVE_UP = 3
_resume_failures = 0
# 인증 문제(토큰 만료·취소, 401)는 다시 시도해도 소용없고 사람이 토큰을 갱신해야 한다.
# 새 세션으로 넘어가지 않고(기억 보존), 말로 알리고, daijin/brain/alert 에 retained 로 남긴다.
_AUTH_FAIL = re.compile(r"401|unauthori[sz]ed|authentication|invalid.{0,20}(api key|token)|token.{0,20}(expired|revoked|invalid)|not logged in|please run /login", re.I)
AUTH_FAIL_SENTENCE = "브레인 인증에 문제가 생겼어. 서버에서 토큰을 확인해 줘."

def _read_turn(lines, st):
    """claude 의 stream-json 출력을 한 턴(result 이벤트까지) 읽어 문장을 yield. 결과는 st 에 남긴다:
    ok(정상 종료) · yielded(한 문장이라도 나옴) · noise(JSON 이 아닌 출력과 오류 문구, 진단용).
    lines 는 줄 이터레이터: 한 번 쓰고 버리는 프로세스의 stdout 이든, 상주 프로세스의 stdout 이든 같다.
    result 를 보면 멈춘다(상주 프로세스는 다음 턴에 이어 읽는다). 스트림이 그 전에 끝나면 남은 글을 마지막 문장으로 낸다."""
    buf = ""
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            if line.strip():
                st["noise"].append(line.strip()[:300])
            continue
        if not isinstance(obj, dict):
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
                        st["yielded"] = True
                        yield sent
            elif ev.get("type") == "content_block_stop":
                # 텍스트 블록이 끝나면(다음은 도구 호출) 마침표 뒤 공백이 없어도 바로 내보낸다.
                # 안 그러면 "응, 알았어."가 도구 실행이 끝난 뒤 다음 문장과 붙어서 나온다(실측).
                sent = EMOJI.sub("", buf).strip(); buf = ""
                if sent:
                    st["yielded"] = True
                    yield sent
        elif t == "result":
            st["ok"] = not obj.get("is_error")
            if st["ok"]:
                save_session(obj.get("session_id"))
            else:
                st["noise"].append(str(obj.get("result") or obj.get("subtype") or "is_error")[:300])
            break
    rest = EMOJI.sub("", buf).strip()
    if rest and not st["timeout"]:
        st["yielded"] = True
        yield rest

def _claude_stream(text, resume_id, st):
    """claude -p 를 한 턴만 돌려(한 번 쓰고 버리는 프로세스) 문장을 yield. 결과는 st 에 (_read_turn 참고, timeout 도 여기서)."""
    cmd = [CLAUDE, "-p", text, "--output-format", "stream-json",
           "--include-partial-messages", "--verbose",
           "--allowedTools", ALLOWED_TOOLS, "--append-system-prompt", sys_prompt()] + _speed_opts()
    if resume_id:
        cmd += ["--resume", resume_id]
    # stderr 를 stdout 에 합친다: 인증 만료나 세션 없음 같은 오류는 JSON 이 아닌 줄로 나오므로 noise 로 모인다.
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                         stderr=subprocess.STDOUT, text=True, cwd=VOICE, env=_claude_env())
    def _kill():
        st["timeout"] = True
        p.kill()
    killer = threading.Timer(BRAIN_TIMEOUT_S, _kill)
    killer.daemon = True
    killer.start()
    try:
        yield from _read_turn(p.stdout, st)
        p.wait()
    finally:
        killer.cancel()
        if p.poll() is None:                # 읽던 쪽이 도중에 그만뒀다: claude 를 남겨 두지 않는다
            p.kill(); p.wait()
        p.stdout.close()

# ---------- 상주 claude ----------
# 턴마다 claude 를 새로 띄우면 기동에만 3초 넘게 든다 (2026-10-31 맥 실측, 같은 한 줄 질문: 새 프로세스 5.0초, 상주 프로세스의
# 두 번째 턴 0.7초). 그래서 프로세스 하나를 띄워 두고 --input-format stream-json 으로 턴을 흘려 넣는다. 세션은 그 프로세스가
# 쥐고 있고(result 의 session_id 를 예전처럼 .daijin_session 에 저장), 죽거나 시간을 넘기면 다음 턴에 --resume 로 다시 띄운다.
# secrets BRAIN_RESIDENT=0 이면 예전처럼 턴마다 새 프로세스 (되돌리기).
def resident_on():
    return sec("BRAIN_RESIDENT") != "0"

class Resident:
    def __init__(self):
        self.p, self.sid, self.turns = None, None, 0

    def alive(self):
        return self.p is not None and self.p.poll() is None

    def start(self, resume_id):
        cmd = [CLAUDE, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
               "--include-partial-messages", "--verbose",
               "--allowedTools", ALLOWED_TOOLS, "--append-system-prompt", sys_prompt()] + _speed_opts()
        if resume_id:
            cmd += ["--resume", resume_id]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, encoding="utf-8", cwd=VOICE, env=_claude_env())
        self.sid, self.turns = resume_id, 0
        print(f"  🧠 claude 상주 시작 (pid {self.p.pid}, 세션 {resume_id or '새로'})")

    def stop(self, why=""):
        p, self.p = self.p, None
        if p is None:
            return
        for f in (p.stdin,):
            try: f.close()
            except Exception: pass
        if p.poll() is None:
            try: p.wait(timeout=3)
            except subprocess.TimeoutExpired: p.kill(); p.wait()
        try: p.stdout.close()
        except Exception: pass
        print(f"  🧠 claude 상주 종료 ({why or '정리'}, {self.turns}턴)")

    def _drain(self, st):
        """죽은 프로세스가 남긴 출력(오류 문구)을 noise 로 거둔다."""
        try:
            for line in self.p.stdout:
                if line.strip():
                    st["noise"].append(line.strip()[:300])
        except Exception:
            pass

    def turn(self, text, resume_id, st):
        """한 턴. 문장을 yield. 세션이 바뀌었거나 프로세스가 없으면 (다시) 띄운다."""
        if self.alive() and self.sid != resume_id:
            self.stop("세션 바뀜")
        if not self.alive():
            self.start(resume_id)
        p = self.p
        try:
            p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}, ensure_ascii=False) + "\n")
            p.stdin.flush()
        except (BrokenPipeError, OSError) as e:          # 뜨자마자 죽었다 (--resume 한 세션이 없을 때 등): 이유는 stdout 에 있다
            self._drain(st); self.stop(f"쓰기 실패 {e.__class__.__name__}")
            return
        def _kill():
            st["timeout"] = True
            p.kill()
        killer = threading.Timer(BRAIN_TIMEOUT_S, _kill)
        killer.daemon = True
        killer.start()
        try:
            yield from _read_turn(p.stdout, st)
        finally:
            killer.cancel()
        self.turns += 1
        if st["ok"]:
            self.sid = load_session()                    # result 의 session_id 가 저장됐다: 다음 턴의 비교 기준
        if st["timeout"] or p.poll() is not None:
            self._drain(st)
            self.stop("시간 초과" if st["timeout"] else f"종료 코드 {p.returncode}")

_resident = Resident()
import atexit
atexit.register(lambda: _resident.stop("브레인 종료"))

def _think_turn(text, resume_id, st):
    if resident_on():
        yield from _resident.turn(text, resume_id, st)
    else:
        yield from _claude_stream(text, resume_id, st)

def _new_state():
    return {"ok": False, "yielded": False, "timeout": False, "noise": []}

def claude_think(text, out):
    """Claude 로 한 턴. 문장을 yield 한다. 끝까지 아무 말도 못 하고 실패하면 out["failed"] 에 이유를 남기고 사과 문장은 내지 않는다
    (다음 엔진으로 넘어갈 수 있게). 시간 초과와 인증 실패는 그 자체가 답이라 문장을 낸다."""
    global _resume_failures
    sid = load_session()
    st = _new_state()
    text = with_state(text)
    yield from _think_turn(text, sid, st)
    if st["timeout"]:
        print(f"  ⚠️ 에이전트 {BRAIN_TIMEOUT_S}초 초과 → 중단")
        out["failed"] = "timeout"
        return
    if st["ok"] or st["yielded"]:
        _resume_failures = 0
        return
    print(f"  ⚠️ 에이전트 실패: {' | '.join(st['noise'][-3:]) or '출력 없음'}")
    if any(_AUTH_FAIL.search(n) for n in st["noise"]):
        _alert("auth", "claude CLI 인증 실패: " + (st["noise"][-1][:160] if st["noise"] else ""))
        out["failed"] = "auth"
        return
    gone = bool(sid) and any(_SESSION_GONE in n for n in st["noise"])
    if sid and not gone:
        _resume_failures += 1
    if gone or (sid and _resume_failures >= RESUME_GIVE_UP):
        print("  (세션을 찾을 수 없음 → 새 세션으로)" if gone else
              f"  (세션 재개가 {_resume_failures}번 연속 실패 → 새 세션으로. 이전 id 는 .daijin_session.prev)")
        st = _new_state()
        yield from _think_turn(text, None, st)
        if st["ok"] or st["yielded"]:
            _resume_failures = 0
            return
        print(f"  ⚠️ 새 세션도 실패: {' | '.join(st['noise'][-3:]) or '출력 없음'}")
    out["failed"] = "error"

# ---------- 생각 엔진 ----------
# secrets 의 BRAIN_ENGINE: claude(기본) · local · 또는 "claude,local" 처럼 쉼표로 순서를 주면 앞의 엔진이 아무 말도 못 하고 실패할 때
# 다음 엔진으로 넘어간다. local 은 OpenAI 호환 엔드포인트(Ollama 등)에 붙는 local_mind.py. 외부 AI 서비스 없이 생각하는 길.
LOCAL_HISTORY_TURNS = 8
LOCAL_HISTORY_S = 48 * 3600      # "어제 무슨 얘기 했지?"에 답하려면 하루 넘게 필요하다

def engines():
    names = [e.strip() for e in (sec("BRAIN_ENGINE") or "claude").split(",") if e.strip()]
    return [e for e in names if e in ("claude", "local")] or ["claude"]

DAY_WORDS = (("그저께", 2), ("그제", 2), ("엊그제", 2), ("어제", 1), ("어젯밤", 1), ("오늘", 0))
DAY_TURNS = 6

def local_history(text="", now=None):
    """로컬 엔진의 대화 기억 (Claude 의 고정 세션 대신). (history, day_note) 를 돌려준다.
    history: 최근 이틀의 에이전트 턴 최대 8개. 지금과 똑같은 질문의 예전 턴은 뺀다 (작은 모델이 예전 답을 그대로 베낀다).
    day_note: 말에 "어제", "그저께" 같은 날이 나오면 그날의 대화를 최대 6턴 요약한 참고 글 (시스템 프롬프트에 붙는다)."""
    now = time.time() if now is None else now
    norm = lambda t: re.sub(r"[\s.,!?~]", "", t or "")
    try:
        eps = [e for e in selfdb.episodes_since(now - LOCAL_HISTORY_S) if e["kind"] in ("voice", "ask") and e["user_text"] and e["reply"]
               and norm(e["user_text"]) != norm(text)]
        recent = eps[-LOCAL_HISTORY_TURNS:]
        day = next((d for w, d in DAY_WORDS if w in (text or "")), None)
        note = ""
        if day is not None:
            lt = time.localtime(now)
            start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)) - day * 86400
            those = [e for e in selfdb.episodes_since(start) if e["ts"] < start + 86400 and e["kind"] == "voice"
                     and e["user_text"] and e["reply"] and norm(e["user_text"]) != norm(text)][-DAY_TURNS:]
            word = next(w for w, d in DAY_WORDS if w in (text or ""))
            label = f"{word}({time.strftime('%m월 %d일', time.localtime(start))})"
            if those:
                note = (f" [{label} 대화 기록, 사람이 그날 일을 물으면 이걸 보고 답해] " +
                        " / ".join(f"{time.strftime('%H:%M', time.localtime(e['ts']))} 사람: {e['user_text'][:40]} → 너: {e['reply'][:40]}" for e in those))
            else:
                note = f" [{label} 대화 기록: 없음]"
    except Exception:
        return [], ""
    return [(e["user_text"], e["reply"]) for e in recent], note

def local_think(text, out):
    if not local_mind.reachable():
        print(f"  (로컬 모델에 닿지 않음: {local_mind.url()})")
        out["failed"] = "unreachable"
        return
    try:
        history, day_note = local_history(text)
        reply = local_mind.think(text, system_extra=f" [지금: {fastpath.now_line()}]" + state_block() + day_note, history=history)
    except Exception as e:
        print(f"  ⚠️ 로컬 모델 실패: {e!r}")
        out["failed"] = "error"
        return
    reply = EMOJI.sub("", reply).strip()
    if not reply:
        out["failed"] = "empty"
        return
    yield from split_sentences(reply)

def split_sentences(text):
    buf = text
    while True:
        m = _SENT_END.search(buf)
        if not m:
            break
        sent = buf[:m.end()].strip(); buf = buf[m.end():]
        if sent:
            yield sent
    if buf.strip():
        yield buf.strip()

_THINKERS = {"claude": claude_think, "local": local_think}
_last_engine = {"name": None}

def brain_stream(text):
    """생각 한 턴. 엔진을 순서대로 시도하고, 한 문장이라도 나온 엔진에서 멈춘다."""
    order = engines()
    for i, name in enumerate(order):
        out = {}
        said = False
        for sent in _THINKERS[name](text, out):
            said = True
            yield sent
        if said:
            _last_engine["name"] = name
            return
        reason = out.get("failed", "empty")
        if i + 1 < len(order):
            print(f"  (생각 엔진 {name} 실패: {reason} → {order[i + 1]} 로)")
            continue
        _last_engine["name"] = None
        if reason == "timeout":
            yield "미안, 너무 오래 걸려서 멈췄어."
        elif reason == "auth":
            yield AUTH_FAIL_SENTENCE
        else:
            yield "미안, 지금 생각이 잘 안 돼. 다시 말해줄래?"

# ---------- 명령 지름길 ----------
def fast_commands_on():
    v = sec("FAST_COMMANDS")
    if v in ("0", "1"):
        return v == "1"
    return engines()[0] == "local"          # 기본: 로컬 엔진이 앞일 때만 (Claude 경로의 동작은 바꾸지 않는다)

def try_fastpath(text):
    """규칙으로 분명한 명령이면 실행하고 말할 문장을 돌려준다. 아니면 None."""
    if not fast_commands_on():
        return None
    clock = fastpath.answer_clock(text)
    if clock:
        print(f"  ⚡ 지름길(시계): {clock}")
        return clock
    parsed = fastpath.parse(text)
    if not parsed:
        return None
    node, cmd = parsed
    try:
        r = subprocess.run(["bash", os.path.join(VOICE, "dev.sh"), node, cmd], capture_output=True, text=True, timeout=40)
        out = (r.stdout + r.stderr).strip()
        ok = r.returncode == 0 and out.startswith("OK")
    except Exception as e:
        out, ok = str(e), False
    print(f"  ⚡ 지름길: {node} <- {cmd} → {out.splitlines()[0][:80] if out else ''}")
    return fastpath.reply_for(node, cmd, ok, out)

def mp3_to_pcm16k(mp3):
    """mp3 바이트 → PCM 16k mono 16bit 바이트. ffmpeg 파이프(맥·리눅스 공통), 없으면 macOS 내장 afconvert."""
    if shutil.which(FFMPEG):
        r = subprocess.run([FFMPEG, "-loglevel", "error", "-f", "mp3", "-i", "pipe:0",
                            "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1"],
                           input=mp3, capture_output=True, timeout=15, check=True)
        return r.stdout
    if IS_MAC and os.path.exists(AFCONVERT):
        with tempfile.TemporaryDirectory() as d:
            src, dst = f"{d}/tts.mp3", f"{d}/tts.wav"
            with open(src, "wb") as f:
                f.write(mp3)
            subprocess.run([AFCONVERT, "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", src, dst],
                           check=True, capture_output=True, timeout=15)
            return wav_to_pcm(dst)
    raise RuntimeError("ffmpeg 없음 (리눅스: apt install ffmpeg)")

# ---------- TTS 엔진: 각각 PCM 16k 조각을 yield, 실패는 예외로 ----------
def _tts_edge(text):
    """MS 뉴럴 Seraphina, 무료 (맥·리눅스 공통). 브레인 프로세스 안에서 바로 부른다:
    문장마다 파이썬을 새로 띄우던 때보다 문장당 0.5초 이상 빠르다 (2026-10-01 서버 실측 0.85~4.3초 → 0.24~0.43초)."""
    if edge_tts is None:
        raise RuntimeError("edge-tts 미설치 (pip install edge-tts)")
    async def fetch():
        async def collect():
            out = bytearray()
            async for ch in edge_tts.Communicate(text, EDGE_VOICE).stream():
                if ch["type"] == "audio":
                    out += ch["data"]
            return bytes(out)
        return await asyncio.wait_for(collect(), 20)
    mp3 = asyncio.run(fetch())
    if not mp3:
        raise RuntimeError("빈 오디오")
    yield mp3_to_pcm16k(mp3)

def _tts_eleven(text):
    """ElevenLabs 커스텀 보이스, 크레딧제(월 1만 무료). PCM 을 받는 대로 흘려보낸다."""
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}/stream?output_format=pcm_16000",
        data=json.dumps({"text": text, "model_id": "eleven_flash_v2_5"}).encode(),
        headers={"xi-api-key": sec("ELEVEN_API_KEY"), "Content-Type": "application/json",
                 "User-Agent": "daijin-brain/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        while True:
            d = resp.read(8192)
            if not d:
                break
            yield d

def f32_44k_to_pcm16k(samples):
    """Supertonic 출력(float32 44.1kHz mono numpy) → PCM 16k 16bit. ffmpeg 파이프 (맥·리눅스 공통)."""
    import numpy as np
    raw = np.asarray(samples, dtype=np.float32).ravel().tobytes()
    r = subprocess.run([FFMPEG, "-loglevel", "error", "-f", "f32le", "-ar", "44100", "-ac", "1", "-i", "pipe:0",
                        "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1"],
                       input=raw, capture_output=True, timeout=15, check=True)
    return r.stdout

_supertonic = {"tts": None, "voice": None, "style": None}

def _tts_supertonic(text):
    """Supertonic 3 (Supertone, 99M, ONNX, CPU). 한국어 포함 31개 언어, 외부 호출 없음.
    2026-10-10 실측: 한국어 RTF 맥 M1 Pro 0.13, Hetzner 2vCPU 0.18~0.23 (4 steps). 모델은 첫 호출에 3~4초 로드 후 상주.
    보이스는 내장 프리셋 M1~M5, F1~F5 (secrets SUPERTONIC_VOICE, 기본 M1). 품질·속도 조절은 SUPERTONIC_STEPS (기본 4)."""
    if supertonic is None:
        raise RuntimeError("supertonic 미설치 (pip install supertonic)")
    if _supertonic["tts"] is None:
        _supertonic["tts"] = supertonic.TTS()
    voice = sec("SUPERTONIC_VOICE") or "M1"
    if _supertonic["voice"] != voice:
        _supertonic["style"] = _supertonic["tts"].get_voice_style(voice)
        _supertonic["voice"] = voice
    try:
        steps = int(sec("SUPERTONIC_STEPS") or "4")
    except ValueError:
        steps = 4
    wav, _dur = _supertonic["tts"].synthesize(text, _supertonic["style"], total_steps=steps, lang="ko")
    yield f32_44k_to_pcm16k(wav)

def _tts_say(text):
    """macOS 내장 Yuna. 네트워크 없이도 되는 맥 전용 최종 폴백."""
    with tempfile.TemporaryDirectory() as d:
        out = f"{d}/say.wav"
        subprocess.run(["/usr/bin/say", "-v", TTS_VOICE, "-o", out,
                        "--file-format=WAVE", "--data-format=LEI16@16000", text], check=True, timeout=30)
        yield wav_to_pcm(out)

_TTS_ENGINES = {"supertonic": _tts_supertonic, "edge": _tts_edge, "eleven": _tts_eleven, "yuna": _tts_say}

def _tts_order():
    """secrets 의 TTS_ENGINE(기본 edge)을 먼저, 그다음은 쓸 수 있는 나머지 엔진 순서대로.
    supertonic 은 패키지가 깔려 있을 때만 후보가 된다 (로컬이라 폴백 순서에서도 앞쪽)."""
    first = sec("TTS_ENGINE") or "edge"
    order = [first] + [e for e in ("supertonic", "edge", "eleven", "yuna") if e != first]
    usable = {"supertonic": supertonic is not None, "edge": True, "eleven": bool(sec("ELEVEN_API_KEY")), "yuna": IS_MAC}
    return [e for e in order if e in _TTS_ENGINES and usable[e]]

def tts_stream(text):
    """PCM 16k 조각을 yield. 엔진은 secrets.local.txt 의 TTS_ENGINE 으로 고른다 (매번 다시 읽으므로 재시작 불필요):
      supertonic (로컬, pip install supertonic) · edge (기본) · eleven (ELEVEN_API_KEY 필요) · yuna (macOS 전용)
    고른 엔진이 실패하면 쓸 수 있는 다음 엔진으로 넘어간다. edge 는 비공식 엔드포인트라 가끔 끊겨서 한 번 더 시도한다.
    전부 실패하면 아무것도 내보내지 않는다 (그 문장은 건너뛴다)."""
    if not text:
        text = "잘 못 들었어, 다시 말해줄래?"
    for name in _tts_order():
        for attempt in range(2 if name == "edge" else 1):
            sent = False
            try:
                for pcm in _TTS_ENGINES[name](text):
                    sent = True
                    yield pcm
                if sent:
                    return
                raise RuntimeError("빈 오디오")
            except Exception as e:
                print(f"  (TTS {name} 실패: {e})")
                if sent:
                    return                   # 일부가 이미 나갔으면 다른 엔진으로 처음부터 다시 말하지 않는다
    print(f"  ⚠️ TTS 전부 실패, 이 문장은 건너뜀: {text[:40]!r}")

# ---------- 오디오 청킹 (docs/chunking-protocol.md) ----------
# 메시지 = [clipId:1][seq:2 BE][flags:1(bit0=last)][페이로드]
# 다운스트림(audio/out)은 IMA ADPCM 4:1 압축 — EU 브로커 RTT 300ms × ESP32 TCP창 5.7KB
# = 최대 ~19KB/s라 PCM(32KB/s)은 물리적으로 불가. ADPCM은 8KB/s로 여유.
CHUNK_OUT = 2048      # ADPCM 청크 (=4096샘플=256ms). 디바이스가 올려 보내는 청크도 같은 크기다
_rx = {"clip": None, "parts": {}, "at": 0}   # 수신 중인 클립 재조립 버퍼 (at = 마지막 청크 시각)

def rx_abandon(reason):
    """받다 만 클립을 버리고 로그에 남긴다. TCP 위라 중간 청크가 빠지는 일은 드물고, 실제로는 기기 연결이 끊겨 꼬리가 안 오는 쪽이다
    (2026-10-31 실측: 밖에서 건 세 번째 말은 기기 쪽 발행 실패로 아예 안 왔고, 그 전 둘은 온전했다)."""
    if _rx["clip"] is None:
        return
    print(f"  ⚠️ 클립 {_rx['clip']} 미완성: {len(_rx['parts'])}청크 받고 {reason}")
    _rx["clip"], _rx["parts"] = None, {}

RX_STALE_S = 10                      # 마지막 청크 뒤 이만큼 조용하면 꼬리를 잃은 것으로 본다 (클립은 길어야 20초, 청크 간격은 0.3초쯤)

# ---------- IMA ADPCM 코덱과 WAV: audio_codec.py (테스트와 도구가 dm.AdpcmEnc 처럼 부를 수 있게 이름을 그대로 둔다) ----------
from audio_codec import _STEP, _IDX, AdpcmEnc, AdpcmDec, pcm_to_wav, wav_to_pcm  # noqa: E402,F401

def publish_pcm_stream(client, pcm_iter):
    """PCM 조각 이터레이터를 받는 즉시 ADPCM 청크로 발행 (TTS 생성과 전송을 겹침)"""
    clip = int(time.time()) & 0xFF
    try:
        vol = float(sec("TTS_VOLUME") or "0.6")
    except ValueError:
        vol = 0.6
    enc, buf, seq = AdpcmEnc(vol), b"", 0
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

def publish_no_reply(client):
    """답할 것이 없다는 신호: 페이로드가 빈 마지막 청크 하나. 디바이스는 이걸 받으면 생각하는 얼굴을 풀고 대기로 돌아간다.
    (이게 없으면 버린 클립마다 디바이스가 90초 동안 답을 기다린다.) 이 신호를 모르는 예전 펌웨어는 그냥 무시한다."""
    client.publish(T_AUDIO_OUT, bytes([int(time.time()) & 0xFF, 0, 0, 1 | 2]), qos=0)

def on_connect(client, userdata, flags, reason_code, properties):
    print(f"✅ 우체국({broker()}) 연결 (rc={reason_code}) → 구독 {T_AUDIO_IN}, {T_SAY}, {T_ASK}")
    client.subscribe(T_AUDIO_IN, qos=1)
    client.subscribe(T_SAY, qos=1)
    client.subscribe(T_ASK, qos=1)
    client.subscribe(T_DEV_STATUS, qos=0)      # 지금 상태 블록용 (retained 라 접속 즉시 마지막 값이 온다)
    client.subscribe(T_NODE_STATUS, qos=0)
    publish_status(client)                 # 접속 즉시 online (끊겼을 때 남은 will 의 offline 을 덮는다)

SPEECH_RMS = 40    # 16bit PCM 상위창 RMS. 2026-09-18 실측: 실제 발화가 212밖에 안 나와 260은 너무 높았음. 진짜 무음(0~20)만 거른다

def speech_rms(pcm):
    a = array.array("h"); a.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not a: return 0.0
    # 20ms 창별 RMS의 상위 10% 평균 → 짧게라도 말한 구간이 있으면 잡힌다
    win = 320; vals = []
    for i in range(0, len(a) - win, win):
        seg = a[i:i+win]; vals.append(math.sqrt(sum(x*x for x in seg) / win))
    vals.sort(); top = vals[-max(1, len(vals)//10):]
    return sum(top) / len(top)

def handle_clip(client, pcm):
    t0 = time.monotonic()
    turn = Turn("voice", t0)
    turn.info["clip_s"] = round(len(pcm) / 32000, 2)
    rms = speech_rms(pcm)
    turn.info["rms"] = int(rms)
    if rms < SPEECH_RMS:
        print(f"  🔇 말소리 없음(RMS {rms:.0f} < {SPEECH_RMS}) → 클립 무시")
        publish_no_reply(client)
        turn.info["dropped"] = "silence"; turn.done(client)
        return
    with tempfile.TemporaryDirectory() as d:
        wav = f"{d}/clip.wav"
        pcm_to_wav(pcm, wav)
        heard, logprob = stt(wav)
    turn.mark("stt")
    if logprob is not None: turn.info["stt_conf"] = round(logprob, 2)
    user = normalize_nodes(heard)
    conf = "" if logprob is None else f"  (음량 {rms:.0f}, 신뢰도 {logprob:.2f})"     # 잡음 클립을 가르는 기준을 잡을 때 볼 값
    if user and _looks_hallucinated(user, logprob):
        print(f"  🔇 환청 의심(프롬프트 예문 반복): {user!r}{conf} → 클립 무시")
        publish_no_reply(client)
        turn.info["dropped"] = "hallucination"; turn.done(client)
        return
    print(f"  🗣️  나: {user}{conf}")
    turn.text_in = user
    client.publish(T_TXT_IN, user)
    if user and intercepted(client, user, "voice", t0):
        turn.info["path"] = "hook"; turn.done(client)
        return
    # 문장 완성 즉시 TTS→발행 (완전 스트리밍 파이프라인)
    fast = try_fastpath(user) if user else None
    if fast:
        turn.info["path"] = "fast"
        speak_stream(client, iter([fast]), t0, turn)
        return
    src = brain_stream(with_proactive_context(user)) if user else iter(["잘 못 들었어, 다시 말해줄래?"])
    speak_stream(client, src, t0, turn)

# 말하기 직전 치환 — 프롬프트로 안 고쳐지는 습관어. TTS가 "ack"를 "액"으로 읽어 "액바닷서"가 됨(2026-09-16).
def _no_batchim(word):
    """마지막 글자에 받침이 없으면 True (한글이 아니면 False). 조사 고르기용: 레드가/레드는, 그린이/그린은."""
    c = word[-1]
    return "가" <= c <= "힣" and (ord(c) - 0xAC00) % 28 == 0
_OPEN_NAMES = "|".join(re.escape(d["name"]) for d in NODES.values() if _no_batchim(d["name"]))   # 받침 없는 이름: 레드|블루
_SPEAK_FIX = [
    (re.compile(r"(명령(?:을|은)?\s*)?보냈고\s*(?:둘|전부|모두|다)?\s*(?:ack|에크|애크|확인\s*응답)\s*(?:을|도)?\s*받았어"), "보냈어"),
    (re.compile(r"(?:ack|에크|애크)\s*(?:을|도|은|는|가)?\s*(?:받았어|왔어|받음|확인했어)"), "전달됐어"),
    (re.compile(r"(?:ack|에크|애크)\s*(?:를|을|도|은|는|가|이)?\s*(?:안\s*줘|없어|안\s*와|못\s*받았어)"), "응답이 없어"),
    (re.compile(r"\back\b", re.I), "응답"),
    # 노드 호칭: dev1/dev2, 1번(dev1) 같은 표기가 새어 나오면 전부 "1번"/"2번"으로 (TTS가 "대부 2"로 읽음)
    (re.compile(rf"([{_DIGITS}])번\s*\(\s*dev\s*[{_DIGITS}]\s*\)"), r"\1번"),
    (re.compile(rf"(?<![A-Za-z0-9])dev\s*([{_DIGITS}])(?![0-9])", re.I), r"\1번"),
    # "번" 뒤 조사 정리: 받침 있는 "번" 뒤엔 이/은/을/이랑
    (re.compile(r"번가(?=\s|$)"), "번이"), (re.compile(r"번는(?=\s|$)"), "번은"), (re.compile(r"번를(?=\s|$)"), "번을"), (re.compile(r"번랑(?=\s|$)"), "번이랑"),
]
# 음성 호칭은 이름 (voice/nodes.json): 1번 → 레드, 2번 → 블루
_SPEAK_FIX += [(re.compile(rf"(?<![0-9]){d['n']}번"), d["name"]) for d in NODES.values()]
if _OPEN_NAMES:   # 받침 없는 이름은 조사를 바꾼다: 1번이/은/을/이랑 → 레드가/는/를/랑
    _SPEAK_FIX += [
        (re.compile(rf"({_OPEN_NAMES})이랑"), r"\1랑"), (re.compile(rf"({_OPEN_NAMES})이(?=\s|$|[,.!?])"), r"\1가"),
        (re.compile(rf"({_OPEN_NAMES})은(?=\s|$|[,.!?])"), r"\1는"), (re.compile(rf"({_OPEN_NAMES})을(?=\s|$|[,.!?])"), r"\1를"),
    ]
def speak_fix(t):
    for pat, rep in _SPEAK_FIX:
        if "\\1" in rep:                                  # 역참조가 있는 치환은 그대로
            t = pat.sub(rep, t)
        else:
            t = pat.sub(lambda m: (m.group(1) or "") + rep if m.re.groups else rep, t)
    return re.sub(r"\s{2,}", " ", t).strip()

# ---------- 브레인 상태·알림·계측 ----------
_client = None            # main() 이 세팅. 워커 밖(brain_stream)에서 알림을 띄울 때 쓴다
_started = time.time()
_turns = 0
_alerts = {}              # kind -> 마지막으로 알린 시각 (같은 문제를 매 턴 떠들지 않게)
ALERT_COOLDOWN_S = 600

def _alert(kind, text, cooldown=None):
    """사람이 봐야 할 문제. retained 로 남겨 두어 나중에 붙는 대시보드도 본다. 해결되면 clear_alert()."""
    now = time.time()
    if now - _alerts.get(kind, 0) < (ALERT_COOLDOWN_S if cooldown is None else cooldown):
        return
    _alerts[kind] = now
    print(f"  🚨 alert[{kind}]: {text}")
    if _client is not None:
        _client.publish(T_BRAIN_ALERT, json.dumps({"kind": kind, "text": text, "ts": int(now)}, ensure_ascii=False), qos=1, retain=True)

def clear_alert():
    if _client is not None:
        _client.publish(T_BRAIN_ALERT, b"", qos=1, retain=True)

# ---------- 인증 토큰 만료 ----------
# `claude setup-token` 의 1년 토큰은 스스로 갱신되지 않고, 만료 경고도 /login 로그인에만 뜬다(환경 변수 토큰에는 안 뜬다).
# 만료일은 발급할 때 사람이 secrets 에 적어 둔다(CLAUDE_TOKEN_EXPIRES=YYYY-MM-DD). 30일 전부터 하루 한 번 알리고, 상태 블록에도 적어
# 다음 대화에서 다이진이 직접 말하게 한다. 조용히 죽는 길(2026-10 리서치 §인프라 5)을 막는 가장 싼 방법이다.
TOKEN_WARN_DAYS = 30

def token_days_left(now=None):
    """만료까지 남은 날 수(실수, 지났으면 음수). CLAUDE_TOKEN_EXPIRES 가 없거나 날짜가 아니면 None."""
    s = sec("CLAUDE_TOKEN_EXPIRES")
    if not s:
        return None
    try:
        exp = time.mktime(time.strptime(s[:10], "%Y-%m-%d"))
    except ValueError:
        return None
    return (exp - (time.time() if now is None else now)) / 86400

def token_line(now=None):
    """상태 블록에 넣을 한 줄. 30일 안이 아니면 ""."""
    d = token_days_left(now)
    if d is None or d > TOKEN_WARN_DAYS:
        return ""
    when = f"{int(d)}일 뒤" if d >= 1 else ("오늘" if d >= 0 else f"{int(-d)}일 전에")
    return (f"내 인증(클로드 토큰)이 {when} 만료{'돼' if d >= 0 else '됐어'}: 사람에게 `claude setup-token` 으로 새 토큰을 만들어 "
            "secrets 의 CLAUDE_CODE_OAUTH_TOKEN 과 CLAUDE_TOKEN_EXPIRES 를 바꿔 달라고 할 것 (오늘 이미 말했으면 다시 말하지 않는다)")

def token_watch(now=None):
    """heartbeat 가 부른다: 30일 안이면 하루 한 번 alert(retained)."""
    d = token_days_left(now)
    if d is None or d > TOKEN_WARN_DAYS:
        return
    text = f"클로드 토큰 만료 {int(d)}일 전" if d >= 0 else f"클로드 토큰 만료됨 ({int(-d)}일 전)"
    _alert("token", text + ": claude setup-token 으로 갱신하고 secrets 의 CLAUDE_TOKEN_EXPIRES 도 바꿀 것", cooldown=86400)

def brain_status(online=True):
    return json.dumps({"online": online, "ts": int(time.time()), "up_s": int(time.time() - _started),
                       "turns": _turns, "engine": ",".join(engines()),
                       "model": (sec("BRAIN_MODEL") or DEFAULT_MODEL) if engines()[0] == "claude" else local_mind.model(),
                       "stt": _stt_engine(), "tts": (_tts_order() or ["?"])[0],
                       "host": socket.gethostname()}, ensure_ascii=False)

def publish_status(client):
    client.publish(T_BRAIN_STATUS, brain_status(), qos=1, retain=True)

def heartbeat(client):
    """60초마다 상태를 retained 로 올리고, HEARTBEAT_URL(secrets, 선택)이 있으면 거기도 찍는다:
    Uptime Kuma 의 Push 모니터나 healthchecks.io 같은 곳. 브레인이 죽으면 그쪽이 알린다."""
    while True:
        time.sleep(60)
        try:
            if _rx["clip"] is not None and time.time() - _rx["at"] > RX_STALE_S:
                rx_abandon(f"{int(time.time() - _rx['at'])}초 조용함 (기기 연결이 끊긴 듯)")
            publish_status(client)
            token_watch()
            url = sec("HEARTBEAT_URL")
            if url:
                urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "daijin-brain/1.0"}), timeout=10).read()
        except Exception as e:
            print(f"  (heartbeat 실패: {e})")

class Turn:
    """한 턴의 단계별 시각. 끝나면 JSON 한 줄을 로그와 daijin/brain/turn 에 남긴다.
    kind: voice(말로 들어옴) | ask | say. 시간은 전부 t0(클립 디코드 완료 또는 메시지 수신) 기준 초."""
    def __init__(self, kind, t0=None):
        self.kind, self.t0 = kind, (time.monotonic() if t0 is None else t0)
        self.marks, self.info = {}, {}
        self.text_in, self.text_out = None, None   # selfdb.episodes 에 남길 들린 말·한 말
    def mark(self, name):
        self.marks.setdefault(name, round(time.monotonic() - self.t0, 2))
    def done(self, client):
        global _turns
        _turns += 1
        self.mark("done")
        d = {"kind": self.kind, **self.info, **self.marks, "ts": int(time.time())}
        line = json.dumps(d, ensure_ascii=False)
        print(f"  ⏱ turn {line}")
        if not self.info.get("dropped"):            # 무음·환청으로 버린 클립은 대화가 아니다
            selfdb.episode(self.kind, user_text=self.text_in, reply=self.text_out, stt_conf=self.info.get("stt_conf"),
                           rms=self.info.get("rms"), clip_s=self.info.get("clip_s"), turn_json=line)
        try:
            client.publish(T_BRAIN_TURN, line, qos=0)
        except Exception as e:
            print(f"  (turn 발행 실패: {e})")

def speak_stream(client, sentence_iter, t0, turn=None):
    """문장 이터레이터를 스트리밍 TTS→ADPCM 발행. 음성 응답과 능동 발화의 공통 출구.
    turn 이 있으면 첫 문장(first_sent), 첫 소리(first_audio), 끝(done)을 기록한다."""
    sentences = []
    def gen():
        for s in sentence_iter:
            s = speak_fix(s)
            if turn: turn.mark("first_sent")
            sentences.append(s)
            print(f"  🤖 daijin: {s}  (+{time.monotonic()-t0:.1f}s)")
            for pcm in tts_stream(s):
                if turn: turn.mark("first_audio")
                yield pcm
            yield b"\x00" * 3200              # 문장 사이 0.1초 숨고르기
    n = publish_pcm_stream(client, gen())
    client.publish(T_TXT_OUT, " ".join(sentences))
    if turn: turn.text_out = " ".join(sentences)
    print(f"  📤 답 발행 {n} 청크 · ⏱️ {time.monotonic()-t0:.1f}s")
    if turn:
        turn.info.update(sentences=len(sentences), chunks=n)
        turn.done(client)

def handle_say(client, text):
    """daijin/say — 받은 텍스트를 그대로 말한다 (에이전트 미경유, 알림·차임용)"""
    t0 = time.monotonic()
    print(f"\n📢 say: {text}")
    speak_stream(client, iter([text]), t0, Turn("say", t0))

def handle_ask(client, text):
    """daijin/ask — 상황 텍스트를 에이전트에 넣고, 다이진이 스스로 표현해 말한다.
    세션 고정을 그대로 타므로 대화 맥락·기억도 이어진다 (보고·능동 알림용)"""
    t0 = time.monotonic()
    print(f"\n📨 ask: {text}")
    client.publish(T_TXT_IN, f"[ask] {text}")
    if intercepted(client, text, "ask", t0):
        return
    turn = Turn("ask", t0); turn.text_in = text
    speak_stream(client, brain_stream(with_proactive_context(text)), t0, turn)

# 처리(STT/Claude/TTS/발행)는 워커 스레드에서 — paho 네트워크 스레드(콜백)에서 하면
# publish가 콜백 종료까지 실제 송신되지 않아 스트리밍이 무효화됨
_clipq = queue.Queue()

def worker(client):
    while True:
        kind, data = _clipq.get()
        try:
            if   kind == "pcm": handle_clip(client, data)
            elif kind == "say": handle_say(client, data)
            elif kind == "ask": handle_ask(client, data)
        except Exception as e:
            print(f"  ⚠️ 처리 오류: {e!r}")
            _alert("worker", f"{kind} 처리 중 예외: {e!r}"[:200])
            try:
                publish_no_reply(client)          # 디바이스가 생각하는 얼굴로 90초 기다리지 않게
            except Exception:
                pass

def on_message(client, userdata, msg):
    if msg.topic == T_SAY:
        _clipq.put(("say", msg.payload.decode("utf-8", "replace").strip()))
        return
    if msg.topic == T_ASK:
        _clipq.put(("ask", msg.payload.decode("utf-8", "replace").strip()))
        return
    if msg.topic == T_DEV_STATUS or msg.topic.startswith("daijin/dev/"):
        note_status(msg.topic, msg.payload)
        return
    if msg.topic != T_AUDIO_IN:
        return
    p = msg.payload
    if len(p) <= 4:
        return
    clip, seq, last = p[0], (p[1] << 8) | p[2], p[3] & 1
    if _rx["clip"] != clip or seq == 0:       # 새 클립 시작 (seq 0 도 기준: 디바이스가 재부팅하면 clipId 가 다시 0부터다)
        rx_abandon("다음 클립이 왔다")
        _rx["clip"], _rx["parts"] = clip, {}
        _rx["adpcm"] = bool(p[3] & 2)         # bit1 = ADPCM 압축 클립
    _rx["at"] = time.time()
    _rx["parts"][seq] = p[4:]
    if last:
        gap = sorted(set(range(seq + 1)) - _rx["parts"].keys())
        if gap:                               # QoS 0 라 중간 청크가 빠질 수 있다. ADPCM 은 상태 기반이라 그 뒤 소리가 일그러진다
            print(f"  ⚠️ 클립 {clip}: 청크 {len(gap)}개 유실 (seq {', '.join(map(str, gap[:5]))}{' …' if len(gap) > 5 else ''})")
        data = b"".join(_rx["parts"][k] for k in sorted(_rx["parts"]))
        if _rx.get("adpcm"):
            data = AdpcmDec().decode(data)    # 4:1 해제 → 원본 PCM
        print(f"\n🎧 클립 {clip} 수신: {len(_rx['parts'])} 청크, "
              f"{len(data)} bytes{' (ADPCM)' if _rx.get('adpcm') else ''}")
        _rx["clip"], _rx["parts"] = None, {}
        _clipq.put(("pcm", data))

def main():
    global _client
    c = cloud_client("daijin-brain")
    c.will_set(T_BRAIN_STATUS, brain_status(online=False), qos=1, retain=True)   # 죽으면 브로커가 offline 으로 표시
    c.on_connect = on_connect
    c.on_message = on_message
    _client = c
    threading.Thread(target=worker, args=(c,), daemon=True).start()
    threading.Thread(target=heartbeat, args=(c,), daemon=True).start()
    print(f"🤖 daijin 브레인 시작 — 우체국 {broker()}, 생각 {','.join(engines())} — 연결 중...")
    if sec("CLAUDE_CODE_OAUTH_TOKEN") and token_days_left() is None:
        print("  (secrets 에 CLAUDE_TOKEN_EXPIRES=YYYY-MM-DD 를 적어 두면 토큰 만료 30일 전부터 미리 알린다)")
    connect_with_retry(c)
    c.loop_forever()

if __name__ == "__main__":
    main()
