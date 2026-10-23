#!/usr/bin/env python3
# 다이진의 자아가 놓이는 자리: 대화(episodes), 먼저 말 건 기록(asks, 거절 장부), 몸의 변수(drives), 추동별 정책(policy),
# 가중치 밖의 기억(memories), 인격의 버전(persona_versions). 설계는 docs/design-self-layer.md.
#
# SQLite 한 파일(기본 voice/daijin.db, secrets SELF_DB 로 바꿀 수 있음), WAL. 브레인·initiative·reflect 가 같이 쓴다.
# 다른 모듈은 전부 이 파일의 함수로만 DB 를 만진다. DB 가 없거나 깨져도 호출한 쪽이 멈추지 않도록, 쓰기 함수는 예외를 밖으로 내지
# 않고 None 을 돌려준다 (기록 못 한 것은 로그에만).
import json, os, sqlite3, threading, time
from daijin_common import VOICE, sec

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
  id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, kind TEXT NOT NULL,
  user_text TEXT, reply TEXT, stt_conf REAL, rms INTEGER, clip_s REAL, turn_json TEXT,
  ask_id INTEGER REFERENCES asks(id));
CREATE INDEX IF NOT EXISTS episodes_ts ON episodes(ts);
CREATE TABLE IF NOT EXISTS asks (
  id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, drive TEXT NOT NULL, drive_value REAL, trigger TEXT,
  channel TEXT NOT NULL, source TEXT NOT NULL, text TEXT NOT NULL, presence TEXT,
  response_class TEXT, response_ts INTEGER, latency_s REAL, response_text TEXT,
  repair_strategy TEXT, repair_outcome TEXT, wanted_to_ask_less INTEGER, label_source TEXT);
CREATE INDEX IF NOT EXISTS asks_ts ON asks(ts);
CREATE TABLE IF NOT EXISTS drives (ts INTEGER NOT NULL, name TEXT NOT NULL, value REAL NOT NULL, inputs TEXT);
CREATE INDEX IF NOT EXISTS drives_ts ON drives(ts);
CREATE TABLE IF NOT EXISTS policy (
  drive TEXT PRIMARY KEY, base_interval_s INTEGER NOT NULL, backoff_level INTEGER NOT NULL DEFAULT 0,
  last_ask_ts INTEGER, muted_until INTEGER, avoid_hours TEXT);
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
  source_episode INTEGER, importance INTEGER, valid_from INTEGER, valid_to INTEGER, confidence REAL);
CREATE TABLE IF NOT EXISTS persona_versions (
  id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, text TEXT NOT NULL, diff_summary TEXT,
  proposed_by TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notes (ts INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS node_state (node TEXT PRIMARY KEY, online INTEGER NOT NULL, since INTEGER NOT NULL, excused_at INTEGER);
"""

RESPONSE_CLASSES = ("engaged", "acknowledged", "refused_context", "negative", "unanswered")

_local = threading.local()
_path_override = None

def db_path():
    return _path_override or sec("SELF_DB") or os.path.join(VOICE, "daijin.db")

def use(path):
    """테스트·도구용: 이 프로세스가 쓸 DB 파일을 바꾼다 (secrets 보다 우선)."""
    global _path_override
    _path_override = path
    _close()

def _close():
    c = getattr(_local, "conn", None)
    if c is not None:
        c.close()
    _local.__dict__.clear()

def db():
    """스레드마다 연결 하나. 처음 열 때 스키마를 만든다(멱등)."""
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", None) != db_path():
        if c is not None:
            _close()                           # 다른 파일로 바뀌었다: 옛 연결을 닫는다
        c = sqlite3.connect(db_path(), timeout=5, isolation_level=None)   # autocommit; 여러 프로세스가 같이 쓴다
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        c.executescript(SCHEMA)
        try:                                   # 예전 DB: node_state 에 excused_at 이 없다
            c.execute("ALTER TABLE node_state ADD COLUMN excused_at INTEGER")
        except sqlite3.OperationalError:
            pass
        _local.conn, _local.path = c, db_path()
    return c

def _safe(fn):
    """쓰기 함수 래퍼: DB 문제로 브레인이 멈추지 않게 한다."""
    def wrap(*a, **kw):
        try:
            return fn(*a, **kw)
        except sqlite3.Error as e:
            print(f"  (selfdb {fn.__name__} 실패: {e})")
            return None
    wrap.__name__ = fn.__name__
    return wrap

# ---------- episodes ----------
@_safe
def episode(kind, user_text=None, reply=None, stt_conf=None, rms=None, clip_s=None, turn_json=None, ask_id=None, ts=None):
    cur = db().execute("INSERT INTO episodes(ts,kind,user_text,reply,stt_conf,rms,clip_s,turn_json,ask_id) VALUES(?,?,?,?,?,?,?,?,?)",
                       (int(ts or time.time()), kind, user_text, reply, stt_conf, rms, clip_s, turn_json, ask_id))
    return cur.lastrowid

def episodes_since(ts, kinds=None):
    q, args = "SELECT * FROM episodes WHERE ts >= ?", [int(ts)]
    if kinds:
        q += " AND kind IN (%s)" % ",".join("?" * len(kinds)); args += list(kinds)
    return db().execute(q + " ORDER BY ts", args).fetchall()

def last_voice_ts():
    r = db().execute("SELECT MAX(ts) AS t FROM episodes WHERE kind='voice' AND user_text IS NOT NULL AND user_text != ''").fetchone()
    return r["t"] if r and r["t"] else None

@_safe
def link_unlinked_episodes(window_s=15):
    """응답으로 닫힌 ask 에 아직 episode 가 연결되지 않았으면, 응답 시각 근처의 voice episode 를 연결한다.
    브레인은 턴이 끝난 뒤에 episode 를 쓰고 initiative 는 text/in 을 보는 즉시 장부를 닫으므로, 연결은 나중에 시각으로 맞춘다."""
    c = db()
    rows = c.execute("""SELECT a.id AS ask_id, a.response_ts FROM asks a
                        WHERE a.response_class IS NOT NULL AND a.response_class != 'unanswered'
                          AND NOT EXISTS (SELECT 1 FROM episodes e WHERE e.ask_id = a.id)
                        ORDER BY a.ts DESC LIMIT 20""").fetchall()
    n = 0
    for r in rows:
        e = c.execute("""SELECT id FROM episodes WHERE kind='voice' AND ask_id IS NULL AND ts BETWEEN ? AND ?
                         ORDER BY ABS(ts - ?) LIMIT 1""", (r["response_ts"] - window_s, r["response_ts"] + window_s + 60, r["response_ts"])).fetchone()
        if e:
            c.execute("UPDATE episodes SET ask_id=? WHERE id=?", (r["ask_id"], e["id"])); n += 1
    return n

@_safe
def link_episode(episode_id, ask_id):
    db().execute("UPDATE episodes SET ask_id=? WHERE id=?", (ask_id, episode_id))
    return True

# ---------- asks (거절 장부) ----------
@_safe
def open_ask(drive, channel, text, source, trigger=None, drive_value=None, presence="unknown", ts=None):
    cur = db().execute("INSERT INTO asks(ts,drive,drive_value,trigger,channel,source,text,presence) VALUES(?,?,?,?,?,?,?,?)",
                       (int(ts or time.time()), drive, drive_value, trigger, channel, source, text, presence))
    return cur.lastrowid

def open_asks():
    """아직 응답이 적히지 않은 것, 오래된 순."""
    return db().execute("SELECT * FROM asks WHERE response_class IS NULL ORDER BY ts").fetchall()

@_safe
def close_ask(ask_id, response_class, response_text=None, response_ts=None, label_source="rule"):
    assert response_class in RESPONSE_CLASSES, response_class
    row = db().execute("SELECT ts FROM asks WHERE id=?", (ask_id,)).fetchone()
    if row is None:
        return None
    rts = int(response_ts or time.time())
    latency = None if response_class == "unanswered" else max(0, rts - row["ts"])
    db().execute("UPDATE asks SET response_class=?, response_ts=?, latency_s=?, response_text=?, label_source=? WHERE id=?",
                 (response_class, rts, latency, response_text, label_source, ask_id))
    return True

@_safe
def relabel_ask(ask_id, response_class=None, wanted_to_ask_less=None):
    """밤 반성이 규칙의 분류를 고치거나 프랑크퍼트 칸을 채운다."""
    if response_class is not None:
        assert response_class in RESPONSE_CLASSES, response_class
        db().execute("UPDATE asks SET response_class=?, label_source='reflect' WHERE id=?", (response_class, ask_id))
    if wanted_to_ask_less is not None:
        db().execute("UPDATE asks SET wanted_to_ask_less=? WHERE id=?", (1 if wanted_to_ask_less else 0, ask_id))
    return True

@_safe
def set_repair(ask_id, strategy, outcome=None):
    db().execute("UPDATE asks SET repair_strategy=?, repair_outcome=? WHERE id=?", (strategy, outcome, ask_id))
    return True

def recent_asks(n=5, drive=None):
    q, args = "SELECT * FROM asks", []
    if drive:
        q += " WHERE drive=?"; args.append(drive)
    return db().execute(q + " ORDER BY ts DESC LIMIT ?", args + [n]).fetchall()

def asks_since(ts, source=None):
    q, args = "SELECT * FROM asks WHERE ts >= ?", [int(ts)]
    if source:
        q += " AND source=?"; args.append(source)
    return db().execute(q + " ORDER BY ts", args).fetchall()

# ---------- drives / policy ----------
@_safe
def record_drive(name, value, inputs=None, ts=None):
    db().execute("INSERT INTO drives(ts,name,value,inputs) VALUES(?,?,?,?)",
                 (int(ts or time.time()), name, float(value), json.dumps(inputs, ensure_ascii=False) if inputs is not None else None))
    return True

def latest_drives():
    rows = db().execute("SELECT d.* FROM drives d JOIN (SELECT name, MAX(ts) t FROM drives GROUP BY name) m ON d.name=m.name AND d.ts=m.t").fetchall()
    return {r["name"]: r for r in rows}

def policy_get(drive, base_interval_s):
    row = db().execute("SELECT * FROM policy WHERE drive=?", (drive,)).fetchone()
    if row is None:
        db().execute("INSERT OR IGNORE INTO policy(drive, base_interval_s) VALUES(?,?)", (drive, int(base_interval_s)))
        row = db().execute("SELECT * FROM policy WHERE drive=?", (drive,)).fetchone()
    return row

@_safe
def policy_update(drive, **fields):
    allowed = {"base_interval_s", "backoff_level", "last_ask_ts", "muted_until", "avoid_hours"}
    bad = set(fields) - allowed
    assert not bad, bad
    if "avoid_hours" in fields and not isinstance(fields["avoid_hours"], (str, type(None))):
        fields["avoid_hours"] = json.dumps(sorted(set(fields["avoid_hours"])))
    sets = ", ".join(f"{k}=?" for k in fields)
    c = db()
    c.execute("INSERT OR IGNORE INTO policy(drive, base_interval_s) VALUES(?, ?)", (drive, int(fields.get("base_interval_s", 3600))))
    c.execute(f"UPDATE policy SET {sets} WHERE drive=?", list(fields.values()) + [drive])
    return True

# ---------- memories ----------
@_safe
def add_memory(kind, text, importance=5, source_episode=None, confidence=None, supersedes=None, ts=None):
    now = int(ts or time.time())
    if supersedes:
        db().execute("UPDATE memories SET valid_to=? WHERE id=? AND valid_to IS NULL", (now, supersedes))
    cur = db().execute("INSERT INTO memories(ts,kind,text,source_episode,importance,valid_from,confidence) VALUES(?,?,?,?,?,?,?)",
                       (now, kind, text, source_episode, int(importance), now, confidence))
    return cur.lastrowid

def top_memories(n=5, max_chars=200):
    """유효한 기억을 중요도·최근 순으로, 글자 상한 안에서."""
    rows = db().execute("SELECT * FROM memories WHERE valid_to IS NULL ORDER BY importance DESC, ts DESC LIMIT ?", (n * 3,)).fetchall()
    out, used = [], 0
    for r in rows:
        if len(out) >= n or used + len(r["text"]) > max_chars:
            break
        out.append(r); used += len(r["text"]) + 2
    return out

def valid_memories():
    return db().execute("SELECT * FROM memories WHERE valid_to IS NULL ORDER BY ts").fetchall()

# ---------- persona ----------
def persona_current():
    return db().execute("SELECT * FROM persona_versions WHERE status='current' ORDER BY id DESC LIMIT 1").fetchone()

def persona_pending():
    return db().execute("SELECT * FROM persona_versions WHERE status='proposed' ORDER BY id").fetchall()

@_safe
def persona_propose(text, diff_summary, proposed_by, ts=None):
    cur = db().execute("INSERT INTO persona_versions(ts,text,diff_summary,proposed_by,status) VALUES(?,?,?,?,'proposed')",
                       (int(ts or time.time()), text, diff_summary, proposed_by))
    return cur.lastrowid

@_safe
def persona_approve(version_id):
    c = db()
    row = c.execute("SELECT * FROM persona_versions WHERE id=?", (version_id,)).fetchone()
    if row is None or row["status"] != "proposed":
        return False
    c.execute("UPDATE persona_versions SET status='superseded' WHERE status='current'")
    c.execute("UPDATE persona_versions SET status='current' WHERE id=?", (version_id,))
    return True

@_safe
def persona_reject(version_id):
    db().execute("UPDATE persona_versions SET status='rejected' WHERE id=? AND status='proposed'", (version_id,))
    return True

@_safe
def persona_set_current(text, by="human", diff_summary=None):
    """사람이 파일을 직접 고친 경우: 제안과 승인을 한 번에."""
    vid = persona_propose(text, diff_summary or "사람이 직접 편집", by)
    return persona_approve(vid) and vid

# ---------- node_state ----------
# 노드가 지금 상태가 된 시각. 브로커의 retained 오프라인 표시에는 시각이 없어서, 재시작 뒤에도 "언제부터 꺼져 있었나"를 알려면 여기 남겨야 한다.
@_safe
def node_state_set(node, online, since):
    """켜지면 '일부러 꺼 둠' 표시도 지운다 (다음에 꺼지면 다시 물어도 된다)."""
    db().execute("INSERT INTO node_state(node, online, since) VALUES(?,?,?) ON CONFLICT(node) DO UPDATE SET online=excluded.online, since=excluded.since,"
                 " excused_at = CASE WHEN excluded.online = 1 THEN NULL ELSE node_state.excused_at END",
                 (node, 1 if online else 0, int(since)))
    return True

@_safe
def node_excuse(node, ts=None):
    """사람이 이 노드는 일부러 꺼 뒀다고 했다: 다시 켜질 때까지 node_loss 가 이 노드를 세지 않는다."""
    db().execute("INSERT INTO node_state(node, online, since, excused_at) VALUES(?,0,?,?) ON CONFLICT(node) DO UPDATE SET excused_at=excluded.excused_at",
                 (node, int(ts or time.time()), int(ts or time.time())))
    return True

def node_excused():
    try:
        return {r["node"] for r in db().execute("SELECT node FROM node_state WHERE excused_at IS NOT NULL AND online = 0")}
    except sqlite3.Error:
        return set()

def node_state_all():
    return {r["node"]: (bool(r["online"]), r["since"]) for r in db().execute("SELECT * FROM node_state")}

# ---------- notes ----------
@_safe
def note(kind, text, ts=None):
    db().execute("INSERT INTO notes(ts,kind,text) VALUES(?,?,?)", (int(ts or time.time()), kind, text))
    return True

if __name__ == "__main__":
    import sys
    c = db()
    print("DB:", db_path())
    for t in ("episodes", "asks", "drives", "memories", "persona_versions", "notes"):
        print(f"  {t}: {c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}")
    if len(sys.argv) > 1 and sys.argv[1] == "asks":
        for r in recent_asks(20):
            print(dict(r))
