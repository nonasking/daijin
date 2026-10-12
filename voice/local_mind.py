#!/usr/bin/env python3
# 로컬 생각 엔진 — Claude 대신 집(또는 어디든)의 모델로 생각한다. 외부 AI 서비스 없이 다이진이 돌기 위한 자리.
#
# 붙는 곳: OpenAI 호환 채팅 엔드포인트면 무엇이든 (Ollama, llama.cpp server, LM Studio, vLLM ...).
#   LOCAL_LLM_URL   (기본 http://127.0.0.1:11434/v1, Ollama)
#   LOCAL_LLM_MODEL (기본 qwen3.5:4b)  · LOCAL_LLM_KEY (필요한 서버만) · LOCAL_LLM_REASONING (none|low|medium|high, 기본 low)
#
# Claude 경로와 다른 점 (2026-10-10 실측: 작은 모델은 도구를 안 부르고 "했다"고 말하거나 명령 이름을 지어낸다):
#   - 셸·파일·웹 도구를 주지 않는다. 도구는 셋뿐: 노드 명령, 플릿 상태, 디바이스 상태.
#   - 노드 명령은 허용된 동사만 받는다. 지어낸 명령이면 실행하지 않고 쓸 수 있는 목록을 돌려줘 다시 고르게 한다.
#   - 몸·노드 상태는 시스템 프롬프트의 "지금 상태"에 이미 들어 있다 (조회할 틈을 줄여 지어내기를 막는다).
#   - 답에 "돌렸어/껐어/줬어" 같은 실행 주장이 있는데 이번 턴에 성공한 명령이 없으면 그 답을 버리고 사실대로 말한다.
#   - 대화 기억은 Claude 세션 대신 selfdb 의 최근 episodes 에서 가져온다.
import json, os, re, socket, subprocess, urllib.parse, urllib.request
from daijin_common import VOICE, sec

DEFAULT_URL = "http://127.0.0.1:11434/v1"
DEFAULT_MODEL = "qwen3.5:4b"
MAX_ROUNDS = 4            # 도구 왕복 상한
TIMEOUT_S = 120

# 노드 펌웨어(15-home-node.ino)의 명령표와 같다. 여기 없는 동사는 보내지 않는다.
VERBS = ("led", "servo", "poke", "step", "motor", "relay", "fan", "buzz", "ping", "caps")
COMMAND_HELP = ("led:<red|green|blue|yellow|cyan|magenta|white|off> · motor (노드 종류 상관없이 모터 한 번) · "
                "servo:<0~180> · poke:<각도>[:ms] · step:<±각도> · relay:on|off|pulse · fan:on|off · "
                "buzz:alarm|ok|fail|<ms> · ping · caps")

TOOLS = [
    {"type": "function", "function": {
        "name": "node_cmd",
        "description": "홈 노드에 명령을 보내고 결과를 받는다. 실제로 기계가 움직이는 유일한 방법이다. 명령: " + COMMAND_HELP,
        "parameters": {"type": "object", "properties": {
            "node": {"type": "string", "description": "red, blue, 또는 all (레드=red, 블루=blue)"},
            "command": {"type": "string", "description": "예: motor, led:off, led:red, buzz:ok"}},
            "required": ["node", "command"]}}},
    {"type": "function", "function": {
        "name": "fleet_status", "description": "모든 홈 노드가 켜져 있는지 지금 다시 조회한다 (지금 상태에 이미 있으면 안 불러도 된다)",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "device_status", "description": "다이진 본체의 온도·신호·켜진 시간을 지금 다시 조회한다",
        "parameters": {"type": "object", "properties": {}}}},
]

SYSTEM = ("너는 'daijin'이라는 이름의 AI 음성 대화 친구야. 따뜻하고 친근하게 한국어 반말로 1~2문장, 짧게 답해. "
          "이모지·마크다운·특수기호는 쓰지 마 (음성으로 읽힌다). "
          "집 노드는 레드와 블루야. 부탁받으면 node_cmd 도구로 직접 실행하고, 도구 결과를 보고 나서 짧게 말해. "
          "규칙: 도구를 실제로 불러 성공하지 않았으면 '돌렸어', '껐어', '줬어'라고 말하지 마. "
          "몸과 노드 상태는 아래 '지금 상태'에 있으니 거기서 답해. 모르는 건 모른다고 해. "
          "너는 웹 검색이나 파일, 셸을 쓸 수 없어. 그런 부탁엔 지금은 못 한다고 짧게 말해. "
          "한국어로만 말해. 한자나 중국어, 영어 문장을 섞지 마.")

# 실행했다는 주장. 이번 턴에 성공한 node_cmd 가 없으면 이런 답은 내보내지 않는다.
CLAIM = re.compile(r"(돌렸|돌려 ?줬|돌아갔|껐|꺼 ?줬|켰|켜 ?줬|줬어|주었|보냈|터뜨렸|울렸|바꿨|바꿔 ?줬|움직였|작동했|실행했)")
NO_ACTION_REPLY = "미안, 방금 건 실제로 실행하지 못했어. 다시 한 번 말해 줄래?"
HAN = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]+")   # 작은 모델이 가끔 섞는 한자 (2026-10-11 실측: "体温 41.5도")

def clean(reply):
    """음성으로 읽힐 답 다듬기: 한자 덩어리를 빼고 공백을 정리한다."""
    if HAN.search(reply):
        print(f"  (로컬 모델 답의 한자 제거: {reply[:40]!r})")
        reply = HAN.sub("", reply)
    return re.sub(r"\s{2,}", " ", reply).strip()

def url():
    return (sec("LOCAL_LLM_URL") or DEFAULT_URL).rstrip("/")

def model():
    return sec("LOCAL_LLM_MODEL") or DEFAULT_MODEL

def reachable(timeout=1.5):
    """엔드포인트 포트가 열려 있나 (자동 선택·폴백 판단용). 사설망 너머(독일 서버 → 한국의 맥, 왕복 0.3초 안팎)도 잡도록 1.5초."""
    u = urllib.parse.urlparse(url())
    try:
        socket.create_connection((u.hostname, u.port or (443 if u.scheme == "https" else 80)), timeout=timeout).close()
        return True
    except OSError:
        return False

def chat(messages, tools=None, json_mode=False, reasoning=None, timeout=TIMEOUT_S):
    """OpenAI 호환 /chat/completions 한 번. 응답 message dict 를 돌려준다."""
    body = {"model": model(), "messages": messages, "temperature": 0.3, "stream": False}
    if tools:
        body["tools"] = tools
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    r = reasoning or sec("LOCAL_LLM_REASONING") or "low"
    if r:
        body["reasoning_effort"] = r          # Ollama: none 이면 생각을 끈다 (2026-10-11 확인)
    headers = {"Content-Type": "application/json", "User-Agent": "daijin-brain/1.0"}
    if sec("LOCAL_LLM_KEY"):
        headers["Authorization"] = f"Bearer {sec('LOCAL_LLM_KEY')}"
    req = urllib.request.Request(url() + "/chat/completions", data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        d = json.loads(resp.read())
    return d["choices"][0]["message"]

# ---------- 도구 실행 ----------
def _run(script, *args, timeout=40):
    env = dict(os.environ)
    r = subprocess.run(["bash", os.path.join(VOICE, script), *args], capture_output=True, text=True, timeout=timeout, env=env)
    return (r.stdout + r.stderr).strip()[-600:], r.returncode

def valid_command(cmd):
    verb = (cmd or "").split(":", 1)[0].strip().lower()
    return verb in VERBS

def run_tool(name, args):
    """(결과 텍스트, 성공한 노드 명령이면 True)."""
    if name == "node_cmd":
        node, cmd = str(args.get("node", "")).strip(), str(args.get("command", "")).strip()
        if not valid_command(cmd):
            return f"실행 안 함: '{cmd}' 는 없는 명령이야. 쓸 수 있는 명령: {COMMAND_HELP}. 맞는 명령으로 다시 불러.", False
        out, rc = _run("dev.sh", node, cmd)
        return out, rc == 0 and out.startswith("OK")
    if name == "fleet_status":
        return _run("fleet.sh")[0], False
    if name == "device_status":
        return _run("status.sh")[0], False
    return f"모르는 도구: {name}", False

# ---------- 한 턴 ----------
def think(text, system_extra="", history=(), run=run_tool):
    """사람의 말 → 다이진의 답(문자열). 도구는 run 으로 실행한다(시험에서 바꿔 끼운다).
    실패(연결 불가, 형식 오류)는 예외로 올린다: 부르는 쪽이 다른 엔진으로 넘어갈 수 있게."""
    who = (f" 지금 너는 이 집 컴퓨터의 로컬 모델({model()})로 생각하고 있어. 예전 대화에서 다른 모델이라고 말한 적이 있어도, "
           "그건 그때 이야기야. 무엇으로 생각하냐고 물으면 지금 사실대로 답해.")
    messages = [{"role": "system", "content": SYSTEM + who + (" " + system_extra if system_extra else "")}]
    for u, a in history:
        messages += [{"role": "user", "content": u}, {"role": "assistant", "content": a}]
    messages.append({"role": "user", "content": text})
    acted = False
    for _ in range(MAX_ROUNDS):
        m = chat(messages, tools=TOOLS)
        calls = m.get("tool_calls") or []
        if not calls:
            reply = (m.get("content") or "").strip()
            if reply and CLAIM.search(reply) and not acted:
                print(f"  (로컬 모델이 실행 없이 실행했다고 함: {reply[:60]!r} → 바로잡음)")
                return NO_ACTION_REPLY
            return clean(reply)
        messages.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
        for c in calls:
            fn = c.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            result, ok = run(fn.get("name"), args)
            acted = acted or ok
            print(f"  🔧 로컬 도구 {fn.get('name')}({args}) → {result.splitlines()[0][:80] if result else ''}")
            messages.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": result})
    return "미안, 생각이 너무 길어졌어. 다시 말해 줄래?"

def complete_json(prompt, timeout=600):
    """도구 없이 JSON 하나를 받는다 (밤의 반성용). 실패는 예외.
    생각 모드는 끈다(LOCAL_REFLECT_REASONING 으로 바꿀 수 있다): 2026-10-11 실측, qwen3.5:4b 는 생각을 켜면 반성 프롬프트에서
    1만 자 넘게 생각하다 Ollama 기본 문맥 길이에 걸려 빈 답을 냈다. 끄면 4초에 올바른 JSON. 생각을 켜려면 Ollama 를
    OLLAMA_CONTEXT_LENGTH=16384 같은 큰 문맥으로 띄운다."""
    m = chat([{"role": "user", "content": prompt}], json_mode=True, reasoning=sec("LOCAL_REFLECT_REASONING") or "none", timeout=timeout)
    return m.get("content") or ""
