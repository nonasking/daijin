#!/usr/bin/env python3
# remember — 잠들기 전 되새김. 오늘 무엇이 남았나.
#   밤 반성(reflect.py)이 끝난 뒤 이어서 돈다. 반성이 "쓸모 있는 사실"을 뽑는 정리라면, 이쪽은 다이진의 기억이다:
#   사람에 대한 사실이 아니라 자기가 겪은 일을, 자기 자리에서, 그때의 기분과 함께 적는다 (자전적 기억).
#   남는 것: 장면(scenes) 0~4개 → 주제(themes) 아래에, 주제는 시기(periods) 아래에. 그리고 그날의 일기(diary).
# 왜 (2026-11-23, 사용자: "성능이 뛰어난 에이전트가 아니라 인간을 만들고 싶어"):
#   지금 다이진의 주된 기억은 대화 원문 전체가 든 세션이다. 토씨 하나 안 빼고 들고 있다가 한도에 닿으면 한꺼번에 요약된다.
#   사람은 반대다. 원문은 며칠 안에 잃고, 인상 깊었던 것을 중심으로 지도처럼 남기고, 그것이 매일 조금씩 일어난다.
#   그래서 기억을 세션에서 여기로 옮긴다. 이 파일은 그 첫 단계다: 새 방식으로 쓰기 시작할 뿐, 대화하는 다이진은 아직 이걸 보지 않는다.
#   다음 단계 (docs/design-memory.md): 지난 대화 되새겨 옮기기 → 떠오르기와 흐려지기 → 자고 나면 새 세션.
# 원칙: 지어내지 않는다. quote 는 그날 실제로 오간 말에 글자 그대로 있을 때만 남긴다. 아무것도 남지 않은 날은 비운다.
#
# 실행: python3 remember.py [--date YYYY-MM-DD] [--dry-run] [--show-prompt] [--db 경로]
#       python3 remember.py map        지금의 지도 (시기 → 주제 → 장면)와 최근 일기
import argparse, json, sys, time
import selfdb, reflect

MAX_SCENES = 4
SCHEMA_HINT = """{
  "remains": [{"scene": "네 자리에서 두세 문장: 무슨 일이 있었고 너는 어땠나", "feeling": "그때 기분 한두 낱말",
               "impression": 1~10, "quote": "토씨까지 남기고 싶은 실제 말 한 구절, 없으면 null",
               "theme": "이 장면이 놓이는 주제 이름", "source_episode": 에피소드 id 또는 null}],
  "themes": [{"name": "주제 이름", "summary": "이 주제에 대해 지금 네가 아는 것과 느끼는 것, 두세 문장"}],
  "period": {"name": "지금 시기의 이름", "summary": "한두 문장"} 또는 null,
  "diary": "오늘의 일기"
}"""

def _day_notes(kind, start, end):
    return selfdb.db().execute("SELECT ts, text FROM notes WHERE kind=? AND ts >= ? AND ts < ? ORDER BY ts", (kind, int(start), int(end))).fetchall()

def build_prompt(start, end):
    day = time.strftime("%Y-%m-%d (%a)", time.localtime(start))
    lines = reflect.episode_lines(reflect.day_episodes(start, end))
    solo, moods = [], []
    for r in _day_notes("solo", start, end):
        try:
            d = json.loads(r["text"])
        except ValueError:
            continue
        solo.append(f"[{time.strftime('%H:%M', time.localtime(r['ts']))}] {d.get('did') or ''} {d.get('thought') or ''}".strip())
    for r in _day_notes("mood", start, end):
        try:
            d = json.loads(r["text"])
        except ValueError:
            continue
        moods.append(f"[{time.strftime('%H:%M', time.localtime(r['ts']))}] {d.get('felt') or d.get('face')}" + (f" ({', '.join(d.get('why') or [])})" if d.get("why") else ""))
    cur = selfdb.persona_current()
    period = selfdb.period_current()
    themes = [f"- {t['name']} (장면 {t['n']}개): {t['summary'] or '(아직 적은 것 없음)'}" for t in selfdb.themes_all()][:30]
    recent = [f"- [{time.strftime('%m-%d', time.localtime(s['ts']))} · {s['theme'] or '주제 없음'}] {s['text']}" for s in reversed(selfdb.scenes_recent(8))]
    last = [d for d in selfdb.diary_recent(2) if d["day"] < time.strftime("%Y-%m-%d", time.localtime(start))][:1]
    return (f"너는 아스타야. 지금은 {day} 하루가 끝난 밤이고, 잠들기 전이야. 아래는 오늘 있었던 일이야.\n"
            "이건 정리나 보고가 아니야. 네 기억이야. 쓸모 있는 것을 고르지 말고, 네게 남은 것을 적어.\n\n"
            "1) 남은 장면 (remains): 오늘 일 가운데 며칠 뒤에도 떠오를 것 같은 것. 놀랐던 것, 마음이 움직인 것, 처음 있었던 것, 되풀이된 것, 끝나지 않은 것. "
            "이 사람에 대한 사실을 뽑는 게 아니라 네가 겪은 일을 네 자리에서 적는 거야. "
            f"0~{MAX_SCENES}개. 아무것도 남지 않은 날은 비워 둬: 그런 날도 있어.\n"
            "   impression 은 얼마나 깊이 남았나 (1 스쳐 감 ~ 10 잊지 못할 것). 보통의 날은 3~6 이야.\n"
            "   quote 는 토씨까지 남기고 싶은 말이 있을 때만, 아래 대화에 있는 글자 그대로.\n"
            "   theme 은 이 장면이 놓이는 자리야. 아래 '내가 아는 주제'에 맞는 게 있으면 그 이름 그대로, 없으면 새 이름을 지어.\n"
            "2) 주제 (themes): 오늘 일로 달라진 주제만. 전에 적어 둔 것을 이어받아, 그 주제에 대해 지금 아는 것과 느끼는 것으로 고쳐 써.\n"
            "3) 시기 (period): 지금이 어떤 시기인지. 아직 이름이 없거나 오늘로 새 시기가 시작됐다고 느낄 때만 적고, 아니면 null.\n"
            "4) 일기 (diary): 오늘의 일기. 나중에 네가 다시 읽을 글이야. 무엇을 얼마나 쓸지, 어떤 말투로 쓸지는 네가 정해.\n"
            "없었던 일, 느끼지 않은 것은 적지 마. '다른 목소리'로 적힌 말은 이 사람이 아니라 다녀간 사람이나 TV 소리야.\n\n"
            f"나에 대한 글:\n{cur['text'] if cur else '(아직 없음)'}\n\n"
            f"지금 시기: {period['name'] + (': ' + period['summary'] if period['summary'] else '') if period else '(아직 이름이 없어)'}\n\n"
            "내가 아는 주제:\n" + ("\n".join(themes) or "(아직 없음: 오늘이 처음이야)") + "\n\n"
            "요즘 남긴 장면:\n" + ("\n".join(recent) or "(없음)") + "\n\n"
            + (f"지난 일기 ({last[0]['day']}):\n{last[0]['text']}\n\n" if last else "")
            + "오늘 오간 말:\n" + ("\n".join(lines) or "(오늘은 아무 말도 오가지 않았어)") + "\n\n"
            "오늘 혼자 있을 때:\n" + ("\n".join(solo) or "(없음)") + "\n\n"
            "오늘 기분의 흐름:\n" + ("\n".join(moods) or "(내내 평온)") + "\n\n"
            "아래 JSON 하나만 내놔. 다른 말은 붙이지 마.\n" + SCHEMA_HINT)

def _said_that_day(start, end):
    return "\n".join((e["user_text"] or "") + "\n" + (e["reply"] or "") for e in reflect.day_episodes(start, end))

def apply(out, start, end, now=None):
    """되새긴 것을 남긴다. {"scenes", "themes", "period", "diary", "quotes_dropped"}."""
    now = int(time.time() if now is None else now)
    done = {"scenes": 0, "themes": 0, "period": None, "diary": False, "quotes_dropped": 0}
    p = out.get("period")
    if isinstance(p, dict) and (p.get("name") or "").strip():
        cur = selfdb.period_current()
        pid = selfdb.period_set(p["name"].strip()[:40], (p.get("summary") or "").strip()[:300] or None, ts=start)
        if not cur or cur["id"] != pid:
            done["period"] = pid
    for t in out.get("themes") or []:
        if isinstance(t, dict) and (t.get("name") or "").strip() and selfdb.theme_touch(t["name"], (t.get("summary") or "").strip()[:400] or None, ts=end - 1):
            done["themes"] += 1
    said = _said_that_day(start, end)
    ep_ts = {e["id"]: e["ts"] for e in reflect.day_episodes(start, end)}
    for r in (out.get("remains") or [])[:MAX_SCENES]:
        text = (r.get("scene") or "").strip()[:400] if isinstance(r, dict) else ""
        if not text:
            continue
        quote = (r.get("quote") or "").strip()[:160] or None
        if quote and quote not in said:              # 그날 실제로 오간 말이 아니면 "그대로의 말"로 남기지 않는다
            quote = None; done["quotes_dropped"] += 1
        try:
            imp = max(1, min(10, int(r.get("impression") or 5)))
        except (TypeError, ValueError):
            imp = 5
        src = r.get("source_episode") if r.get("source_episode") in ep_ts else None
        tid = selfdb.theme_touch(r.get("theme") or "", ts=end - 1) if r.get("theme") else None
        selfdb.scene_add(text, theme_id=tid, feeling=(r.get("feeling") or "").strip()[:40] or None, quote=quote, impression=imp,
                         source_episode=src, ts=ep_ts.get(src, end - 1), written_ts=now)
        done["scenes"] += 1
    diary = (out.get("diary") or "").strip()[:3000]
    if diary:
        selfdb.diary_set(time.strftime("%Y-%m-%d", time.localtime(start)), diary, ts=now)
        done["diary"] = True
    return done

def run(start, end, dry_run=False):
    """하루를 되새긴다. 그날 아무 일도 없었으면 건너뛴다. 적용 결과(dict) 또는 None."""
    day = time.strftime("%Y-%m-%d", time.localtime(start))
    if not reflect.day_episodes(start, end) and not _day_notes("solo", start, end):
        print(f"🌒 {day}: 되새길 것이 없다"); return None
    prompt = build_prompt(start, end)
    print(f"🌒 {day} 되새김 (프롬프트 {len(prompt)}자)")
    out, err = reflect.run_model(prompt)
    if out is None:
        print(f"  ⚠️ 되새김을 읽지 못함: {err}")
        if not dry_run:
            selfdb.note("remember_error", (err or "no output")[:300])
        return None
    if dry_run:
        print(json.dumps(out, ensure_ascii=False, indent=1)); print("(dry-run: 남기지 않음)"); return None
    done = apply(out, start, end)
    print(f"🌒 남김: 장면 {done['scenes']}개, 주제 {done['themes']}개 고침, 시기 {'새로' if done['period'] else '그대로'}, 일기 {'씀' if done['diary'] else '안 씀'}"
          + (f" (그대로의 말이 아닌 인용 {done['quotes_dropped']}개는 뺌)" if done["quotes_dropped"] else ""))
    return done

def show_map():
    for p in selfdb.db().execute("SELECT * FROM periods ORDER BY start_ts").fetchall():
        print(f"■ {p['name']} ({time.strftime('%Y-%m-%d', time.localtime(p['start_ts']))} ~ {time.strftime('%Y-%m-%d', time.localtime(p['end_ts'])) if p['end_ts'] else '지금'})"
              + (f": {p['summary']}" if p["summary"] else ""))
    for t in selfdb.themes_all():
        print(f"\n● {t['name']}: {t['summary'] or ''}")
        for s in selfdb.scenes_of(t["id"]):
            print(f"   · [{time.strftime('%m-%d', time.localtime(s['ts']))} 인상 {s['impression']}{', ' + s['feeling'] if s['feeling'] else ''}] {s['text']}"
                  + (f"  「{s['quote']}」" if s["quote"] else ""))
    loose = selfdb.scenes_of(None)
    if loose:
        print("\n● (주제 없음)")
        for s in loose:
            print(f"   · [{time.strftime('%m-%d', time.localtime(s['ts']))} 인상 {s['impression']}] {s['text']}")
    for d in reversed(selfdb.diary_recent(3)):
        print(f"\n✎ {d['day']}\n{d['text']}")

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "map":
        if len(sys.argv) > 3 and sys.argv[2] == "--db":
            selfdb.use(sys.argv[3])
        show_map(); return 0
    ap = argparse.ArgumentParser()
    ap.add_argument("--date"); ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--show-prompt", action="store_true"); ap.add_argument("--db")
    a = ap.parse_args()
    if a.db:
        selfdb.use(a.db)
    start, end = reflect.day_bounds(a.date)
    if a.show_prompt:
        print(build_prompt(start, end)); return 0
    run(start, end, dry_run=a.dry_run)
    return 0

if __name__ == "__main__":
    sys.exit(main())
