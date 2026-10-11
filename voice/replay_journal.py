#!/usr/bin/env python3
# 지난 브레인 로그(journald)를 selfdb 에 넣어 본다. 장부가 과거 데이터에서 어떻게 분류하는지 눈으로 보는 첫 현실 검증
# (docs/design-self-layer.md §12). 브레인이 episodes 를 직접 쓰기 시작하기 전의 대화도 이걸로 옮긴다.
#
# 입력: journalctl --user -u daijin-brain -o short-unix 의 줄들 (🗣️ 🤖 📢 📨 줄만 있어도 된다)
# 사용:  python3 replay_journal.py journal.txt            → 기본 DB(voice/daijin.db)에 넣는다 (같은 줄은 두 번 넣지 않는다)
#        python3 replay_journal.py journal.txt --dry-run  → 넣지 않고 무엇이 될지만 보여 준다
#        python3 replay_journal.py journal.txt --db /tmp/x.db
import argparse, re, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import selfdb, ledger_rules

LINE = re.compile(r"^(\d+)(?:\.\d+)?\s+\S+\s+\S+:\s*(.*)$")          # short-unix: "ts host unit[pid]: msg"
R_VOICE = re.compile(r"🗣️\s*나:\s*(.*?)(?:\s+\(음량\s*(\d+),\s*신뢰도\s*(-?[\d.]+)\))?\s*$")
R_REPLY = re.compile(r"🤖 daijin:\s*(.*?)(?:\s+\(\+[\d.]+s\))?\s*$")
R_SAY   = re.compile(r"📢 say:\s*(.*)$")
R_ASK   = re.compile(r"📨 ask:\s*(.*)$")
FLEET_SAY = ("방금 꺼졌어", "다시 켜졌어", "소식이 없어", "오프라인이 됐어", "온라인이 됐어")

def parse(lines):
    """줄 → 턴 목록 [{ts, kind, user_text, reply, rms, conf}]. 🤖 줄은 직전 턴의 답에 붙는다."""
    turns = []
    for raw in lines:
        m = LINE.match(raw.strip())
        if not m:
            continue
        ts, msg = int(m.group(1)), m.group(2)
        v = R_VOICE.search(msg)
        if v:
            turns.append({"ts": ts, "kind": "voice", "user_text": v.group(1).strip(), "reply": [],
                          "rms": int(v.group(2)) if v.group(2) else None, "conf": float(v.group(3)) if v.group(3) else None})
            continue
        a = R_ASK.search(msg)
        if a:
            turns.append({"ts": ts, "kind": "ask", "user_text": a.group(1).strip(), "reply": [], "rms": None, "conf": None})
            continue
        s = R_SAY.search(msg)
        if s:
            turns.append({"ts": ts, "kind": "say", "user_text": None, "reply": [s.group(1).strip()], "rms": None, "conf": None})
            continue
        r = R_REPLY.search(msg)
        if r and turns and turns[-1]["kind"] in ("voice", "ask"):
            turns[-1]["reply"].append(r.group(1).strip())
    for t in turns:
        t["reply"] = " ".join(t["reply"]) or None
    return turns

def ask_source(turn):
    text = turn["user_text"] if turn["kind"] == "ask" else turn["reply"]
    if turn["kind"] == "ask" and (text or "").startswith("[시스템"):
        return "fleet", "fleet"
    if turn["kind"] == "say" and any(k in (text or "") for k in FLEET_SAY):
        return "fleet", "fleet"
    return "manual", "unknown"

def replay(turns, dry_run=False, window_s=ledger_rules.RESPONSE_WINDOW_S, now=None):
    """턴을 episodes 로, 먼저 말 건 턴을 asks 로 넣고, 창 안의 다음 voice 턴으로 장부를 닫는다. 결과 요약을 돌려준다."""
    import time as _t
    now = _t.time() if now is None else now
    known = set()
    if not dry_run:
        for e in selfdb.episodes_since(0):
            known.add((e["ts"], e["kind"], e["user_text"], e["reply"]))
    out = {"episodes": 0, "asks": 0, "skipped": 0, "closed": {}}
    pending = []     # (ask_id or None, ask_turn)
    for t in turns:
        key = (t["ts"], t["kind"], t["user_text"], t["reply"])
        if key in known:
            out["skipped"] += 1; continue
        eid = None
        if not dry_run:
            eid = selfdb.episode(t["kind"], user_text=t["user_text"], reply=t["reply"], stt_conf=t["conf"], rms=t["rms"], ts=t["ts"])
        out["episodes"] += 1
        # 먼저 말 건 것(say, ask)은 장부에 연다
        if t["kind"] in ("say", "ask") and (t["reply"] or t["kind"] == "ask"):
            source, drive = ask_source(t)
            spoken = t["reply"] or t["user_text"]
            aid = None if dry_run else selfdb.open_ask(drive, t["kind"], spoken, source, ts=t["ts"])
            pending.append((aid, t))
            out["asks"] += 1
            t["_cls"] = None
        # voice 턴은 열려 있는 장부를 닫는다. 응답은 가장 최근에 말 건 것에 대한 것으로 보고,
        # 그보다 앞서 창 안에 쌓인 것들은 무응답으로 닫는다 (연달아 말 걸면 앞의 것은 묻힌다).
        if t["kind"] == "voice" and t["user_text"]:
            in_window = [(aid, at) for aid, at in pending if t["ts"] - at["ts"] <= window_s]
            pending = [(aid, at) for aid, at in pending if t["ts"] - at["ts"] > window_s]
            for i, (aid, at) in enumerate(in_window):
                last = i == len(in_window) - 1
                cls = ledger_rules.classify(t["user_text"]) if last else "unanswered"
                at["_cls"] = cls
                if not dry_run:
                    selfdb.close_ask(aid, cls, response_text=t["user_text"] if last else None, response_ts=t["ts"])
                    if last:
                        selfdb.link_episode(eid, aid)
                out["closed"][cls] = out["closed"].get(cls, 0) + 1
    for aid, at in pending:                       # 창이 지났는데 아무도 안 닫았다 = unanswered
        if now - at["ts"] > window_s:
            at["_cls"] = "unanswered"
            if not dry_run:
                selfdb.close_ask(aid, "unanswered", response_ts=at["ts"] + window_s)
            out["closed"]["unanswered"] = out["closed"].get("unanswered", 0) + 1
    return out

def main():
    ap = argparse.ArgumentParser(description="journald 브레인 로그 → selfdb")
    ap.add_argument("journal"); ap.add_argument("--db"); ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.db:
        selfdb.use(a.db)
    turns = parse(open(a.journal, encoding="utf-8"))
    res = replay(turns, dry_run=a.dry_run)
    import time
    for t in turns:
        if t["kind"] in ("say", "ask"):
            print(f"{time.strftime('%m-%d %H:%M', time.localtime(t['ts']))}  먼저 말함({t['kind']}): {(t['reply'] or t['user_text'])[:50]!r}  → {t.get('_cls') or '(창 안 응답 없음, 아직 열림)'}")
    print(f"\n{'(dry-run) ' if a.dry_run else ''}episodes {res['episodes']}건, asks {res['asks']}건, 이미 있어 건너뜀 {res['skipped']}건, 분류 {res['closed']}")

if __name__ == "__main__":
    main()
