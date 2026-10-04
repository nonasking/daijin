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

import subprocess, re, os, sys, time, json, wave, threading, queue, shutil, uuid, types
import array, asyncio, math, tempfile
import urllib.request
try:
    import edge_tts                    # pip install edge-tts. 없으면 edge 엔진만 못 쓰고 다음 엔진으로 넘어간다
except ImportError:
    edge_tts = None
from daijin_common import VOICE, REPO, sec, load_hooks, cloud_client, connect_with_retry
from nodes import NODES

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

T_AUDIO_IN  = "daijin/audio/in"
T_AUDIO_OUT = "daijin/audio/out"
T_TXT_IN    = "daijin/text/in"
T_TXT_OUT   = "daijin/text/out"
# 능동 발화 — 다이진이 먼저 말하는 경로 (음성 입력 없이 텍스트로 발화 트리거)
T_SAY       = "daijin/say"    # 페이로드 텍스트를 그대로 TTS → 스피커 (알림·차임용)
T_ASK       = "daijin/ask"    # 페이로드를 에이전트에 전달 → 스스로 표현해 말함 (보고용)

# 허용 도구 — 사용자 결정(2026-07-04)으로 전면 개방. 보안 노트: 브로커 자격증명이
# 곧 브레인 머신의 명령 입력 통로가 되므로, HiveMQ 비밀번호 관리가 곧 보안 경계다.
ALLOWED_TOOLS = "Bash,WebSearch,WebFetch,Read,Glob,Grep,Write,Edit"
# 속도: 모델은 secrets의 BRAIN_MODEL(기본 sonnet-5, 명령 판단엔 충분하고 첫 토큰이 빠름),
# MCP(Gmail 등)는 매 턴 접속 비용만 들어서 끈다 (--strict-mcp-config + 빈 설정).
def _speed_opts():
    return ["--model", sec("BRAIN_MODEL") or "claude-sonnet-5",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']

def _props():
    """각 노드에 물려 있는 물리 소품 설명. 있으면 시스템 프롬프트에 붙인다.
    voice/props.local.txt(저장소에 넣지 않는 이 설치본의 것)가 있으면 그것을, 없으면 voice/props.txt(예시)를 읽는다."""
    for name in ("props.local.txt", "props.txt"):
        try:
            t = open(f"{VOICE}/{name}", encoding="utf-8").read().strip()
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

def load_session():
    try:
        s = open(SESSION_FILE).read().strip()
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
        open(SESSION_FILE + ".prev", "w").write(old)
    open(SESSION_FILE, "w").write(sid)

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
_NAME2N = {w.lower(): d["n"] for d in NODES.values() for w in [d["name"]] + d.get("aliases", [])}
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

def _stt_engine():
    return sec("STT_ENGINE") or ("groq" if sec("GROQ_API_KEY") else "whisper")

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

def stt_groq(wav):
    """(전사, 신뢰도). 신뢰도 = 세그먼트 avg_logprob 의 평균 (0 에 가까울수록 확실, 세그먼트가 없으면 None)."""
    # OpenAI 호환 엔드포인트. prompt는 로컬 whisper와 같은 어휘 바이어스 문장(레드/블루 오인식 교정).
    body, ctype = _multipart({"model": GROQ_MODEL, "language": "ko", "prompt": STT_PROMPT,
                              "response_format": "verbose_json"},      # 세그먼트별 avg_logprob 을 받으려고
                             "clip.wav", open(wav, "rb").read())
    req = urllib.request.Request(GROQ_URL, data=body, method="POST",
                                 # User-Agent 필수: Groq 앞단 Cloudflare가 urllib 기본값(Python-urllib)을 403(1010)으로 막는다
                                 headers={"Authorization": f"Bearer {sec('GROQ_API_KEY')}",
                                          "Content-Type": ctype, "User-Agent": "daijin-brain/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        d = json.loads(resp.read())
    probs = [seg["avg_logprob"] for seg in d.get("segments") or [] if "avg_logprob" in seg]
    return (d.get("text") or "").strip(), (sum(probs) / len(probs) if probs else None)

def stt_whisper(wav):
    r = subprocess.run([WHISPER,"-m",MODEL,"-l","ko","-nt","-np",
                        "--prompt",STT_PROMPT,"-f",wav],
                       capture_output=True, text=True)
    return r.stdout.strip(), None            # whisper-cli 는 신뢰도를 내주지 않는다

def stt(wav):
    """(전사, 신뢰도 또는 None)."""
    engine = _stt_engine()
    if engine == "groq":
        try:
            return stt_groq(wav)
        except Exception as e:
            print(f"  (Groq STT 실패: {e} → 로컬 whisper 폴백)")
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

def _claude_stream(text, resume_id, st):
    """claude -p 를 스트리밍으로 돌려 문장을 yield. 결과는 st 에 남긴다:
    ok(정상 종료) · yielded(한 문장이라도 나옴) · timeout · noise(JSON 이 아닌 출력과 오류 문구, 진단용)."""
    cmd = [CLAUDE, "-p", text, "--output-format", "stream-json",
           "--include-partial-messages", "--verbose",
           "--allowedTools", ALLOWED_TOOLS, "--append-system-prompt", SYS] + _speed_opts()
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
    buf = ""
    try:
        for line in p.stdout:
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
        p.wait()
    finally:
        killer.cancel()
    rest = EMOJI.sub("", buf).strip()
    if rest and not st["timeout"]:
        st["yielded"] = True
        yield rest

def _new_state():
    return {"ok": False, "yielded": False, "timeout": False, "noise": []}

def brain_stream(text):
    global _resume_failures
    sid = load_session()
    st = _new_state()
    yield from _claude_stream(text, sid, st)
    if st["timeout"]:
        print(f"  ⚠️ 에이전트 {BRAIN_TIMEOUT_S}초 초과 → 중단")
        yield "미안, 너무 오래 걸려서 멈췄어."
        return
    if st["ok"] or st["yielded"]:
        _resume_failures = 0
        return
    print(f"  ⚠️ 에이전트 실패: {' | '.join(st['noise'][-3:]) or '출력 없음'}")
    gone = bool(sid) and any(_SESSION_GONE in n for n in st["noise"])
    if sid and not gone:
        _resume_failures += 1
    if gone or (sid and _resume_failures >= RESUME_GIVE_UP):
        print("  (세션을 찾을 수 없음 → 새 세션으로)" if gone else
              f"  (세션 재개가 {_resume_failures}번 연속 실패 → 새 세션으로. 이전 id 는 .daijin_session.prev)")
        st = _new_state()
        yield from _claude_stream(text, None, st)
        if st["ok"] or st["yielded"]:
            _resume_failures = 0
            return
        print(f"  ⚠️ 새 세션도 실패: {' | '.join(st['noise'][-3:]) or '출력 없음'}")
    yield "미안, 지금 생각이 잘 안 돼. 다시 말해줄래?"

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

def _tts_say(text):
    """macOS 내장 Yuna. 네트워크 없이도 되는 맥 전용 최종 폴백."""
    with tempfile.TemporaryDirectory() as d:
        out = f"{d}/say.wav"
        subprocess.run(["/usr/bin/say", "-v", TTS_VOICE, "-o", out,
                        "--file-format=WAVE", "--data-format=LEI16@16000", text], check=True, timeout=30)
        yield wav_to_pcm(out)

_TTS_ENGINES = {"edge": _tts_edge, "eleven": _tts_eleven, "yuna": _tts_say}

def _tts_order():
    """secrets 의 TTS_ENGINE(기본 edge)을 먼저, 그다음은 쓸 수 있는 나머지 엔진 순서대로."""
    first = sec("TTS_ENGINE") or "edge"
    order = [first] + [e for e in ("edge", "eleven", "yuna") if e != first]
    usable = {"edge": True, "eleven": bool(sec("ELEVEN_API_KEY")), "yuna": IS_MAC}
    return [e for e in order if e in _TTS_ENGINES and usable[e]]

def tts_stream(text):
    """PCM 16k 조각을 yield. 엔진은 secrets.local.txt 의 TTS_ENGINE 으로 고른다 (매번 다시 읽으므로 재시작 불필요):
      edge (기본) · eleven (ELEVEN_API_KEY 필요) · yuna (macOS 전용)
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
    print(f"✅ HiveMQ 연결 (rc={reason_code}) → 구독 {T_AUDIO_IN}, {T_SAY}, {T_ASK}")
    client.subscribe(T_AUDIO_IN, qos=1)
    client.subscribe(T_SAY, qos=1)
    client.subscribe(T_ASK, qos=1)

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
    rms = speech_rms(pcm)
    if rms < SPEECH_RMS:
        print(f"  🔇 말소리 없음(RMS {rms:.0f} < {SPEECH_RMS}) → 클립 무시")
        publish_no_reply(client)
        return
    with tempfile.TemporaryDirectory() as d:
        wav = f"{d}/clip.wav"
        pcm_to_wav(pcm, wav)
        heard, logprob = stt(wav)
    user = normalize_nodes(heard)
    conf = "" if logprob is None else f"  (음량 {rms:.0f}, 신뢰도 {logprob:.2f})"     # 잡음 클립을 가르는 기준을 잡을 때 볼 값
    if user and _looks_hallucinated(user, logprob):
        print(f"  🔇 환청 의심(프롬프트 예문 반복): {user!r}{conf} → 클립 무시")
        publish_no_reply(client)
        return
    print(f"  🗣️  나: {user}{conf}")
    client.publish(T_TXT_IN, user)
    if user and intercepted(client, user, "voice", t0):
        return
    # 문장 완성 즉시 TTS→발행 (완전 스트리밍 파이프라인)
    src = brain_stream(user) if user else iter(["잘 못 들었어, 다시 말해줄래?"])
    speak_stream(client, src, t0)

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

def speak_stream(client, sentence_iter, t0):
    """문장 이터레이터를 스트리밍 TTS→ADPCM 발행. 음성 응답과 능동 발화의 공통 출구."""
    sentences = []
    def gen():
        for s in sentence_iter:
            s = speak_fix(s)
            sentences.append(s)
            print(f"  🤖 daijin: {s}  (+{time.monotonic()-t0:.1f}s)")
            for pcm in tts_stream(s):
                yield pcm
            yield b"\x00" * 3200              # 문장 사이 0.1초 숨고르기
    n = publish_pcm_stream(client, gen())
    client.publish(T_TXT_OUT, " ".join(sentences))
    print(f"  📤 답 발행 {n} 청크 · ⏱️ {time.monotonic()-t0:.1f}s")

def handle_say(client, text):
    """daijin/say — 받은 텍스트를 그대로 말한다 (에이전트 미경유, 알림·차임용)"""
    t0 = time.monotonic()
    print(f"\n📢 say: {text}")
    speak_stream(client, iter([text]), t0)

def handle_ask(client, text):
    """daijin/ask — 상황 텍스트를 에이전트에 넣고, 다이진이 스스로 표현해 말한다.
    세션 고정을 그대로 타므로 대화 맥락·기억도 이어진다 (보고·능동 알림용)"""
    t0 = time.monotonic()
    print(f"\n📨 ask: {text}")
    client.publish(T_TXT_IN, f"[ask] {text}")
    if intercepted(client, text, "ask", t0):
        return
    speak_stream(client, brain_stream(text), t0)

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
            print(f"  ⚠️ 처리 오류: {e}")

def on_message(client, userdata, msg):
    if msg.topic == T_SAY:
        _clipq.put(("say", msg.payload.decode("utf-8", "replace").strip()))
        return
    if msg.topic == T_ASK:
        _clipq.put(("ask", msg.payload.decode("utf-8", "replace").strip()))
        return
    if msg.topic != T_AUDIO_IN:
        return
    p = msg.payload
    if len(p) <= 4:
        return
    clip, seq, last = p[0], (p[1] << 8) | p[2], p[3] & 1
    if _rx["clip"] != clip or seq == 0:       # 새 클립 시작 (seq 0 도 기준: 디바이스가 재부팅하면 clipId 가 다시 0부터다)
        _rx["clip"], _rx["parts"] = clip, {}
        _rx["adpcm"] = bool(p[3] & 2)         # bit1 = ADPCM 압축 클립
    _rx["parts"][seq] = p[4:]
    if last:
        data = b"".join(_rx["parts"][k] for k in sorted(_rx["parts"]))
        if _rx.get("adpcm"):
            data = AdpcmDec().decode(data)    # 4:1 해제 → 원본 PCM
        print(f"\n🎧 클립 {clip} 수신: {len(_rx['parts'])} 청크, "
              f"{len(data)} bytes{' (ADPCM)' if _rx.get('adpcm') else ''}")
        _rx["clip"], _rx["parts"] = None, {}
        _clipq.put(("pcm", data))

def main():
    c = cloud_client("daijin-brain")
    c.on_connect = on_connect
    c.on_message = on_message
    threading.Thread(target=worker, args=(c,), daemon=True).start()
    print("🤖 daijin 브레인(MQTT) 시작 — HiveMQ 연결 중...")
    connect_with_retry(c)
    c.loop_forever()

if __name__ == "__main__":
    main()
