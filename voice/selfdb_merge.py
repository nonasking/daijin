#!/usr/bin/env python3
# 비상 모드에서 쌓인 자아 층 기록을 서버 DB 로 합친다 (offline-mode.sh push 가 부른다).
#
# 맥의 비상 모드는 서버 DB 의 복사본(offline-mode.sh sync)에서 시작해 그 위에 대화·장부·기억을 쌓는다.
# 인터넷이 돌아오면 그 사이에 생긴 것만 서버 DB 에 넣는다. 서버도 그동안 계속 돌았을 수 있으니 파일을 덮어쓰지 않고 행을 합친다.
#   - since 이후의 episodes, asks, drives, notes, 새 기억, 새 인격 제안(승인 대기 그대로): 새 id 로 넣고 서로의 참조(episode.ask_id,
#     memory.source_episode)를 새 id 로 바꾼다. 같은 행이 이미 있으면(시각과 내용이 같으면) 건너뛴다: 두 번 돌려도 안전하다.
#   - 비상 모드에서 대체된 기억(valid_to 가 since 이후): 서버의 같은 글 기억도 그때로 닫는다.
#   - 정책: 추동마다 마지막으로 말 건 때가 더 늦은 쪽을 따른다. 피할 시간대는 합친다.
#   - node_state 는 옮기지 않는다 (지금의 상태는 서버가 직접 본다).
#
# 사용: python3 selfdb_merge.py <맥에서 가져온 DB> <서버 DB> <since epoch>
import contextlib, json, sqlite3, sys, time

SYNC_NOTE = "sync"          # notes.kind: offline-mode.sh 가 sync/push 한 때를 남기는 표시. 서버로는 옮기지 않는다

def _rows(c, q, *a):
    return [dict(r) for r in c.execute(q, a).fetchall()]

def _insert(c, table, row):
    cols = [k for k in row if k != "id"]
    cur = c.execute(f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [row[k] for k in cols])
    return cur.lastrowid

def merge(src_path, dst_path, since):
    """src 의 since 이후 기록을 dst 에 합친다. 무엇을 몇 개 넣었는지 dict 로 돌려준다."""
    src = sqlite3.connect(src_path); src.row_factory = sqlite3.Row
    dst = sqlite3.connect(dst_path, timeout=10, isolation_level=None); dst.row_factory = sqlite3.Row
    dst.execute("PRAGMA busy_timeout=10000")
    import selfdb
    dst.executescript(selfdb.SCHEMA)
    done = {"asks": 0, "episodes": 0, "memories": 0, "retired": 0, "drives": 0, "notes": 0, "persona": 0, "policy": 0, "skipped": 0}
    dst.execute("BEGIN IMMEDIATE")
    try:
        ask_map = {}
        for a in _rows(src, "SELECT * FROM asks WHERE ts >= ? ORDER BY ts, id", since):
            same = dst.execute("SELECT id FROM asks WHERE ts=? AND drive=? AND text=?", (a["ts"], a["drive"], a["text"])).fetchone()
            if same:
                ask_map[a["id"]] = same["id"]; done["skipped"] += 1
                continue
            ask_map[a["id"]] = _insert(dst, "asks", a); done["asks"] += 1
        ep_map = {}
        for e in _rows(src, "SELECT * FROM episodes WHERE ts >= ? ORDER BY ts, id", since):
            same = dst.execute("SELECT id FROM episodes WHERE ts=? AND kind=? AND user_text IS ? AND reply IS ?",
                               (e["ts"], e["kind"], e["user_text"], e["reply"])).fetchone()
            if same:
                ep_map[e["id"]] = same["id"]; done["skipped"] += 1
                continue
            e["ask_id"] = ask_map.get(e["ask_id"])           # since 이전 ask 를 가리키면 서버에서의 번호를 모르니 끊는다
            ep_map[e["id"]] = _insert(dst, "episodes", e); done["episodes"] += 1
        for m in _rows(src, "SELECT * FROM memories WHERE ts >= ? ORDER BY ts, id", since):
            if dst.execute("SELECT 1 FROM memories WHERE text=? AND ts=?", (m["text"], m["ts"])).fetchone():
                done["skipped"] += 1
                continue
            m["source_episode"] = ep_map.get(m["source_episode"])
            _insert(dst, "memories", m); done["memories"] += 1
        for m in _rows(src, "SELECT * FROM memories WHERE ts < ? AND valid_to >= ?", since, since):
            cur = dst.execute("UPDATE memories SET valid_to=? WHERE text=? AND valid_to IS NULL", (m["valid_to"], m["text"]))
            done["retired"] += cur.rowcount
        for d in _rows(src, "SELECT * FROM drives WHERE ts >= ?", since):
            if not dst.execute("SELECT 1 FROM drives WHERE ts=? AND name=?", (d["ts"], d["name"])).fetchone():
                _insert(dst, "drives", d); done["drives"] += 1
        for n in _rows(src, "SELECT * FROM notes WHERE ts >= ? AND kind != ?", since, SYNC_NOTE):
            if not dst.execute("SELECT 1 FROM notes WHERE ts=? AND kind=? AND text=?", (n["ts"], n["kind"], n["text"])).fetchone():
                _insert(dst, "notes", n); done["notes"] += 1
        for p in _rows(src, "SELECT * FROM persona_versions WHERE ts >= ? AND status='pending'", since):
            if not dst.execute("SELECT 1 FROM persona_versions WHERE ts=? AND text=?", (p["ts"], p["text"])).fetchone():
                _insert(dst, "persona_versions", p); done["persona"] += 1
        for p in _rows(src, "SELECT * FROM policy"):
            q = dst.execute("SELECT * FROM policy WHERE drive=?", (p["drive"],)).fetchone()
            if q is None:
                _insert(dst, "policy", p); done["policy"] += 1
                continue
            if (p["last_ask_ts"] or 0) <= (q["last_ask_ts"] or 0):
                continue
            hours = sorted(set(json.loads(q["avoid_hours"] or "[]")) | set(json.loads(p["avoid_hours"] or "[]")))
            dst.execute("UPDATE policy SET backoff_level=?, last_ask_ts=?, muted_until=?, avoid_hours=? WHERE drive=?",
                        (p["backoff_level"], p["last_ask_ts"], max(p["muted_until"] or 0, q["muted_until"] or 0) or None,
                         json.dumps(hours) if hours else None, p["drive"]))
            done["policy"] += 1
        dst.execute("COMMIT")
    except Exception:
        dst.execute("ROLLBACK")
        raise
    finally:
        src.close(); dst.close()
    return done

def _open(path):
    """닫히는 연결 (sqlite3 의 with 는 커밋만 하고 닫지 않는다)."""
    return contextlib.closing(sqlite3.connect(path))

def last_sync(path):
    """offline-mode.sh 가 남긴 마지막 sync/push 시각 (없으면 None)."""
    with _open(path) as c:
        try:
            r = c.execute("SELECT MAX(ts) FROM notes WHERE kind=?", (SYNC_NOTE,)).fetchone()
        except sqlite3.Error:
            return None
        return r[0] if r else None

def unpushed(path):
    """마지막 sync/push 뒤에 이 DB 에 생긴 대화·장부·기억 수 (sync 로 덮어쓰기 전에 확인한다)."""
    since = last_sync(path)
    if since is None:
        return 0
    with _open(path) as c:
        return sum(c.execute(f"SELECT COUNT(*) FROM {t} WHERE ts > ?", (since,)).fetchone()[0] for t in ("episodes", "asks", "memories"))

def mark(path, what, ts=None):
    with _open(path) as c:
        c.execute("INSERT INTO notes(ts, kind, text) VALUES(?,?,?)", (int(ts or time.time()), SYNC_NOTE, what))
        c.commit()

if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] in ("since", "unpushed"):
        print({"since": last_sync, "unpushed": unpushed}[sys.argv[1]](sys.argv[2]) or 0)
    elif len(sys.argv) in (4, 5) and sys.argv[1] == "mark":
        mark(sys.argv[2], sys.argv[3], int(sys.argv[4]) if len(sys.argv) == 5 else None)
    elif len(sys.argv) == 4:
        print(json.dumps(merge(sys.argv[1], sys.argv[2], int(sys.argv[3])), ensure_ascii=False))
    else:
        print("사용: selfdb_merge.py <src.db> <dst.db> <since> | since <db> | unpushed <db> | mark <db> pulled|pushed [ts]")
        sys.exit(2)
