#!/usr/bin/env python3
# reflect — 밤에 하루를 돌아본다. 설계: docs/design-self-layer.md §8.
#   입력: 오늘의 episodes, asks, drives 요약, 현재 persona.
#   모델: claude -p 를 대화 세션과 별개로(--resume 없이) 한 번. 도구 없음. 출력은 JSON 하나로 강제.
#   적용: memories 추가(같은 뜻의 옛 기억은 valid_to 를 채워 대체), asks 재분류와 wanted_to_ask_less, persona 변경 제안(승인 대기).
# 실행: python3 reflect.py [--date YYYY-MM-DD] [--dry-run] [--db PATH]   (상시는 systemd 타이머 daijin-reflect, 03:30)
import argparse, json, os, re, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from daijin_common import sec, cloud_client, connect_with_retry
import selfdb

T_PERSONA_PENDING = "daijin/persona/pending"     # retained JSON: 승인 기다리는 인격 제안 수 (대시보드·알림용)
MAX_EPISODES = 120                               # 하루치가 이보다 많으면 최근 것 위주로 자른다

SCHEMA_HINT = """반드시 아래 모양의 JSON 하나만 출력해. 설명이나 마크다운 없이.
{
  "memories": [{"kind": "fact|preference|rule|event", "text": "한 문장", "importance": 1~10, "source_episode": 에피소드 id 또는 null,
                "replaces": "같은 뜻이라 대체하는 기존 기억의 text, 없으면 null"}],
  "ask_labels": [{"ask_id": id, "response_class": "engaged|acknowledged|refused_context|negative|unanswered|null(그대로)",
                  "wanted_to_ask_less": true|false|null}],
  "persona_new": "인격 글 전체(바꿀 게 있을 때만, 없으면 null)",
  "persona_change_summary": "무엇을 왜 바꿨는지 한두 문장, persona_new 가 null 이면 null",
  "note": "오늘 한 줄"
}"""

def _claude_cmd(prompt):
    import daijin_mqtt as dm                     # CLAUDE 경로·인증 환경을 그대로 쓴다 (브로커엔 붙지 않는다)
    model = sec("REFLECT_MODEL") or sec("BRAIN_MODEL") or dm.DEFAULT_MODEL
    cmd = [dm.CLAUDE, "-p", prompt, "--output-format", "json", "--model", model, "--effort", sec("REFLECT_EFFORT") or "medium",
           "--max-turns", "1", "--allowedTools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
    return cmd, dm._claude_env()

def day_bounds(date_str=None, now=None):
    """로컬 날짜 하루의 [시작, 끝) epoch. date_str 이 없으면 어제(03:30 에 도는 타이머 기준으로 '지난 하루')."""
    now = time.time() if now is None else now
    if date_str:
        t = time.strptime(date_str, "%Y-%m-%d")
        start = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    else:
        lt = time.localtime(now - 6 * 3600)      # 새벽에 돌면 "어제", 낮에 손으로 돌리면 "오늘"
        start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    return int(start), int(start) + 86400

def build_prompt(start, end):
    eps = selfdb.episodes_since(start)
    eps = [e for e in eps if e["ts"] < end]
    if len(eps) > MAX_EPISODES:
        eps = eps[-MAX_EPISODES:]
    asks = [a for a in selfdb.asks_since(start) if a["ts"] < end]
    cur = selfdb.persona_current()
    mems = selfdb.valid_memories()
    lines = []
    for e in eps:
        t = time.strftime("%H:%M", time.localtime(e["ts"]))
        if e["kind"] == "voice":
            lines.append(f"[ep{e['id']} {t}] 사람: {e['user_text']}  / 다이진: {e['reply'] or '(답 없음)'}")
        elif e["kind"] == "ask":
            lines.append(f"[ep{e['id']} {t}] (상황이 들어와 다이진이 먼저 말함) {e['reply'] or ''}  ← 상황: {e['user_text']}")
        else:
            lines.append(f"[ep{e['id']} {t}] (다이진이 먼저 말함) {e['reply']}")
    ask_lines = [f"[ask{a['id']} {time.strftime('%H:%M', time.localtime(a['ts']))} {a['source']}/{a['drive']}] 말함: {a['text'][:80]}"
                 f"  → 지금 분류: {a['response_class'] or '열림'}" + (f", 사람 답: {a['response_text'][:60]}" if a["response_text"] else "")
                 for a in asks]
    mem_lines = [f"- ({m['kind']}, 중요도 {m['importance']}) {m['text']}" for m in mems][:40]
    day = time.strftime("%Y-%m-%d", time.localtime(start))
    return (f"너는 'daijin'이라는 AI 음성 친구이고, 지금은 {day} 하루를 돌아보는 밤이다. 아래는 오늘의 대화와 네가 먼저 말 건 기록이다.\n"
            "할 일 셋.\n"
            "1) 기억: 앞으로의 대화에 도움이 될 사실·선호·규칙·사건을 뽑아라. 사람이 한 말에 근거한 것만, 추측은 넣지 마라. 이미 있는 기억과 같은 뜻이면 replaces 로 대체하라. 하루에 0~5개면 충분하다.\n"
            "2) 장부: 네가 먼저 말 건 것(asks)마다, 사람의 답을 다시 읽고 분류가 맞는지 보라. 틀렸으면 고치고, 맞으면 null. 그리고 '그때 덜 묻고 싶었는데 물었는가'를 솔직하게 wanted_to_ask_less 로 답하라. 아무 근거가 없으면 null.\n"
            "3) 인격: 현재 인격 글을 읽고, 오늘 배운 것 때문에 고쳐야 할 문장이 있으면 글 전체를 다시 써서 persona_new 에 넣어라. 바꿀 이유가 분명할 때만. 사람이 승인해야 반영된다.\n\n"
            f"현재 인격 글:\n{cur['text'] if cur else '(아직 없음)'}\n\n"
            f"이미 있는 기억:\n" + ("\n".join(mem_lines) or "(없음)") + "\n\n"
            f"오늘의 대화 {len(eps)}턴:\n" + ("\n".join(lines) or "(없음)") + "\n\n"
            f"오늘 먼저 말 건 기록 {len(asks)}건:\n" + ("\n".join(ask_lines) or "(없음)") + "\n\n" + SCHEMA_HINT)

def extract_json(text):
    """모델 출력에서 첫 JSON 객체를 꺼낸다 (코드 펜스·앞뒤 말 허용)."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None

def reflect_engine():
    """REFLECT_ENGINE 이 있으면 그것, 없으면 브레인의 첫 엔진 (claude | local)."""
    import daijin_mqtt as dm
    e = (sec("REFLECT_ENGINE") or "").strip()
    return e if e in ("claude", "local") else dm.engines()[0]

def run_model(prompt, timeout_s=300):
    """(JSON dict 또는 None, 오류 문구). 엔진은 reflect_engine()."""
    if reflect_engine() == "local":
        import local_mind
        try:
            return extract_json(local_mind.complete_json(prompt, timeout=max(timeout_s, 600))), None
        except Exception as e:
            return None, f"로컬 모델 실패: {e!r}"
    cmd, env = _claude_cmd(prompt)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, env=env, stdin=subprocess.DEVNULL)
    try:
        d = json.loads(r.stdout)
    except ValueError:
        return None, (r.stdout + r.stderr)[-500:]
    if isinstance(d, list):
        d = next((x for x in reversed(d) if isinstance(x, dict) and x.get("type") == "result"), {})
    if d.get("is_error"):
        return None, str(d.get("result"))[:500]
    return extract_json(d.get("result") or ""), None

def apply(out, start, end, now=None):
    """모델 출력(dict) → DB. 무엇을 했는지 요약 dict 로 돌려준다."""
    now = time.time() if now is None else now
    done = {"memories": 0, "replaced": 0, "relabeled": 0, "wanted": 0, "persona": None, "skipped": []}
    valid = {m["text"]: m["id"] for m in selfdb.valid_memories()}
    for m in out.get("memories") or []:
        text = (m.get("text") or "").strip()
        kind = m.get("kind") if m.get("kind") in ("fact", "preference", "rule", "event") else "fact"
        if not text or text in valid:
            done["skipped"].append(text[:30]); continue
        sup = valid.get((m.get("replaces") or "").strip())
        try:
            imp = max(1, min(10, int(m.get("importance") or 5)))
        except (TypeError, ValueError):
            imp = 5
        src = m.get("source_episode") if isinstance(m.get("source_episode"), int) else None
        mid = selfdb.add_memory(kind, text, importance=imp, source_episode=src, supersedes=sup, ts=now)
        if mid:
            valid[text] = mid; done["memories"] += 1; done["replaced"] += 1 if sup else 0
    ids = {a["id"] for a in selfdb.asks_since(start) if a["ts"] < end}
    for lab in out.get("ask_labels") or []:
        aid = lab.get("ask_id")
        if isinstance(aid, str):                  # 작은 모델은 "ask5" 처럼 프롬프트의 표기를 그대로 돌려준다
            digits = re.sub(r"\D", "", aid)
            aid = int(digits) if digits else None
        if aid not in ids:
            continue
        cls = lab.get("response_class")
        if cls not in selfdb.RESPONSE_CLASSES:
            cls = None
        wanted = lab.get("wanted_to_ask_less")
        wanted = wanted if isinstance(wanted, bool) else None
        if cls is not None or wanted is not None:
            selfdb.relabel_ask(aid, response_class=cls, wanted_to_ask_less=wanted)
            done["relabeled"] += 1 if cls else 0; done["wanted"] += 1 if wanted is not None else 0
    new = (out.get("persona_new") or "").strip()
    cur = selfdb.persona_current()
    if new and new != (cur["text"] if cur else ""):
        vid = selfdb.persona_propose(new, out.get("persona_change_summary") or "밤 반성의 제안", "reflect", ts=now)
        done["persona"] = vid
    if out.get("note"):
        selfdb.note("reflect", str(out["note"])[:300], ts=now)
    return done

def publish_pending():
    """승인 기다리는 인격 제안 수를 retained 로 알린다 (없으면 빈 페이로드)."""
    try:
        n = len(selfdb.persona_pending())
        c = cloud_client("daijin-reflect")
        connect_with_retry(c, retry_s=3)
        c.loop_start()
        info = c.publish(T_PERSONA_PENDING, json.dumps({"pending": n, "ts": int(time.time())}) if n else b"", qos=1, retain=True)
        info.wait_for_publish(10)
        c.loop_stop(); c.disconnect()
    except Exception as e:
        print(f"  (알림 발행 실패: {e})")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (기본: 새벽이면 어제, 낮이면 오늘)")
    ap.add_argument("--dry-run", action="store_true", help="모델 출력만 보여 주고 DB 에 적용하지 않는다")
    ap.add_argument("--db")
    ap.add_argument("--show-prompt", action="store_true")
    a = ap.parse_args()
    if a.db:
        selfdb.use(a.db)
    start, end = day_bounds(a.date)
    prompt = build_prompt(start, end)
    if a.show_prompt:
        print(prompt); print("-" * 60)
    if not selfdb.episodes_since(start) and not selfdb.asks_since(start):
        print(f"{time.strftime('%Y-%m-%d', time.localtime(start))}: 돌아볼 것이 없다."); return 0
    print(f"🌙 {time.strftime('%Y-%m-%d', time.localtime(start))} 반성 시작 (엔진 {reflect_engine()}, 프롬프트 {len(prompt)}자)")
    out, err = run_model(prompt)
    if out is None:
        print(f"  ⚠️ 모델 출력을 JSON 으로 읽지 못함: {err}")
        selfdb.note("reflect_error", (err or "no output")[:300])
        return 1
    print(json.dumps(out, ensure_ascii=False, indent=1)[:3000])
    if a.dry_run:
        print("(dry-run: 적용 안 함)"); return 0
    done = apply(out, start, end)
    print(f"✅ 적용: 기억 {done['memories']}개(대체 {done['replaced']}), 장부 재분류 {done['relabeled']}, 덜 묻고 싶었나 {done['wanted']}건, "
          f"인격 제안 {'#%d (승인 대기: python3 voice/persona.py show)' % done['persona'] if done['persona'] else '없음'}")
    publish_pending()
    return 0

if __name__ == "__main__":
    sys.exit(main())
