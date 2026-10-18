#!/usr/bin/env python3
# initiative — 다이진이 먼저 말하는 자리, 그리고 먼저 말한 뒤 무슨 일이 있었는지 적는 자리. 설계: docs/design-self-layer.md §3~§6.
#
#   추동   : 60초마다 몸의 변수(디바이스 상태, 노드 상태, 마지막 대화 경과)에서 0~1 의 "허기"를 계산해 drives 에 기록한다.
#   허용   : 문턱을 넘은 추동마다 필터를 순서대로 통과해야 말한다 (로컬 확장 mute → 조용한 시간 → 하루 예산 → 추동별 간격·
#            피하는 시간대·mute → 브레인이 말하는 중이면 다음 틱). 하나라도 걸리면 왜 안 말했는지 로그에 남긴다.
#   발행   : daijin/say(고정 문장) 또는 daijin/ask(상황을 에이전트에 넘김). INITIATIVE_DRY_RUN=1 이면 발행하지 않고
#            daijin/initiative/plan 에 "지금 말했을 것"만 찍는다. 첫 주는 이 모드로 돌린다.
#   장부   : 버스를 본다. 누가 먼저 말하든(여기, 플릿 워처, say.sh) daijin/say|ask 로 나가므로 그걸 보고 asks 를 열고,
#            daijin/text/in 으로 들어오는 다음 말로 닫아 분류하고(ledger_rules), 정책을 바꾼다. 창이 지나면 unanswered.
#   복구   : 직전이 거절이었던 추동으로 다시 말할 때는 설명이나 약속 한 문장을 앞에 붙인다. 부인은 없다.
#
# 실행: python3 -u initiative.py [--dry-run] [--once]   (상시 구동은 systemd daijin-initiative)
import argparse, json, sys, threading, time
from daijin_common import sec, load_hooks, cloud_client, connect_with_retry
from nodes import NODES, subject, spoken
import selfdb, ledger_rules as rules

T_DEV_STATUS  = "daijin/status"
T_NODE_STATUS = "daijin/dev/+/status"
T_SAY, T_ASK  = "daijin/say", "daijin/ask"
T_TXT_IN, T_TXT_OUT = "daijin/text/in", "daijin/text/out"
T_BRAIN_TURN  = "daijin/brain/turn"
T_PLAN        = "daijin/initiative/plan"

TICK_S = 60
SOURCE = "initiative"

# ---------- 추동 정의 ----------
# value(state) → 0~1. threshold 를 넘으면 말 걸기 후보. base_interval_s 는 같은 추동으로 다시 묻기까지의 기본 간격.
# channel: say = 고정 문장을 그대로, ask = 상황 설명을 에이전트에 넘겨 표현하게 (느리지만 맥락이 붙는다).
DRIVES = {
    "contact":   {"threshold": 0.5, "base_interval_s": 8 * 3600,  "channel": "say"},
    "node_loss": {"threshold": 0.25, "base_interval_s": 24 * 3600, "channel": "ask"},
    "body":      {"threshold": 0.5, "base_interval_s": 2 * 3600,  "channel": "say"},
}
BODY_DANGER = 0.9      # 이 위면 조용한 시간에도 말한다 (온도 68도쯤)
DEVICE_FRESH_S = 180   # 기기는 60초마다 상태를 보낸다. 이만큼 소식이 없으면 꺼진 것으로 보고 말을 걸지 않는다

def clamp(x):
    return max(0.0, min(1.0, x))

class Body:
    """버스에서 받은 마지막 상태. 추동 계산의 입력."""
    def __init__(self):
        self.device, self.device_rx = None, 0
        self.nodes = {}            # id -> (dict, 수신 시각)
        self.offline_since = {}    # id -> 꺼진 시각 (DB 의 node_state 로 재시작을 넘긴다)
        self.last_brain_turn = 0
        try:
            for nid, (online, since) in selfdb.node_state_all().items():
                if not online:
                    self.offline_since[nid] = since
        except Exception as e:
            print(f"  (node_state 읽기 실패: {e})")

    def note(self, topic, payload, now=None):
        now = time.time() if now is None else now
        try:
            d = json.loads(payload)
        except (ValueError, TypeError):
            return
        if topic == T_DEV_STATUS:
            self.device, self.device_rx = d, now
        elif topic.startswith("daijin/dev/"):
            nid = topic.split("/")[2]
            self.nodes[nid] = (d, now)
            if d.get("online"):
                if nid in self.offline_since or nid not in selfdb.node_state_all():
                    selfdb.node_state_set(nid, True, now)
                self.offline_since.pop(nid, None)
            elif nid not in self.offline_since:
                self.offline_since[nid] = now          # 처음 본 오프라인. retained 라면 실제론 더 오래됐을 수 있다
                selfdb.node_state_set(nid, False, now)

def compute_drives(body, last_voice_ts, now=None):
    """이름 → (값, 입력, 말 걸 때 쓸 이유·문장). 입력은 drives.inputs 에 그대로 저장한다."""
    now = time.time() if now is None else now
    out = {}
    # contact: 마지막 대화로부터의 경과. 24시간이면 1.
    gap = (now - last_voice_ts) if last_voice_ts else None
    out["contact"] = (clamp((gap or 0) / 86400), {"since_last_voice_s": int(gap) if gap else None},
                      {"trigger": f"{int((gap or 0) // 3600)}시간째 얘기를 못 해서", "line": "오늘은 아직 얘기를 못 했네."})
    # node_loss: 가장 오래 꺼져 있는 노드. 24시간이면 1. 6시간(0.25)부터 후보.
    worst, worst_h = None, 0.0
    for nid, t0 in body.offline_since.items():
        h = (now - t0) / 3600
        if h > worst_h:
            worst, worst_h = nid, h
    out["node_loss"] = (clamp(worst_h / 24), {"node": worst, "offline_h": round(worst_h, 1)},
                        {"trigger": f"{spoken(worst)}가 {int(worst_h)}시간째 꺼져 있어서" if worst else "",
                         "line": f"[상황] 홈 노드 {spoken(worst)}가 {int(worst_h)}시간째 꺼져 있어. 그냥 둔 건지 짧게 물어봐 줘." if worst else ""})
    # body: 온도와 신호. 온도 50도부터 70도까지 0→1, 신호 -60 부터 -80 까지 0→1. 상태가 오래됐으면 0.
    temp = rssi = None
    if body.device and body.device.get("online") is not False and now - body.device_rx < 300:
        temp, rssi = body.device.get("temp_c"), body.device.get("rssi")
    v_t = clamp(((temp or 0) - 50) / 20) if temp is not None else 0.0
    v_r = clamp((-60 - rssi) / 20) if rssi is not None else 0.0
    hot = v_t >= v_r
    out["body"] = (max(v_t, v_r), {"temp_c": temp, "rssi": rssi},
                   {"trigger": "몸이 뜨거워서" if hot else "신호가 약해서",
                    "line": "나 좀 뜨거워. 바람 좀 쐬게 해 줄래?" if hot else "여기 신호가 약해. 자리를 옮겨 줄 수 있어?"})
    return out

# ---------- 허용 필터 ----------
def budget_used_today(now):
    day_start = now - (time.localtime(now).tm_hour * 3600 + time.localtime(now).tm_min * 60 + time.localtime(now).tm_sec)
    return len(selfdb.asks_since(day_start, source=SOURCE))

def admissible(name, value, body, hooks, now=None):
    """말해도 되면 (True, None), 아니면 (False, 이유). 순서가 곧 우선순위다."""
    now = time.time() if now is None else now
    spec = DRIVES[name]
    if value < spec["threshold"]:
        return False, "threshold"
    fn = getattr(hooks, "mute_reports", None)
    try:
        if fn and fn():
            return False, "muted_by_hooks"
    except Exception:
        pass
    hour = time.localtime(now).tm_hour
    quiet = rules.parse_quiet(sec("INITIATIVE_QUIET") or "23-08")
    if rules.in_quiet(hour, quiet) and not (name == "body" and value >= BODY_DANGER):
        return False, "quiet_hours"
    try:
        budget = int(sec("INITIATIVE_DAILY_BUDGET") or "3")
    except ValueError:
        budget = 3
    if budget_used_today(now) >= budget:
        return False, "daily_budget"
    pol = selfdb.policy_get(name, spec["base_interval_s"])
    if pol is None:
        return False, "no_policy"
    if now < rules.next_allowed_ts(pol):
        return False, "interval"
    if f"{hour:02d}" in rules.avoid_hours(pol):
        return False, "avoid_hour"
    if now - body.last_brain_turn < 30:
        return False, "brain_busy"
    if not device_listening(body, now):
        return False, "device_offline"
    return True, None

def device_listening(body, now):
    """기기가 켜져 있어 말이 실제로 들리나. 꺼진 기기에 한 말은 아무도 못 듣는데 장부엔 '답 없음'으로 남아
    간격만 늘린다 (2026-10-15 08:00 실제로 그랬다: 기기가 꺼진 채 contact 가 말했고 backoff 가 2 로 올랐다)."""
    d = body.device
    return bool(d) and d.get("online") is not False and now - body.device_rx < DEVICE_FRESH_S

# ---------- 장부 관찰 ----------
class Ledger:
    """버스에서 먼저 말한 것을 보고 asks 를 열고, 다음 말로 닫는다. 자기 발행분은 drive 이름을 붙인다."""
    def __init__(self):
        self.pending_label = {}      # text -> (drive, value, trigger, 예약 시각): 내가 방금 발행한 것
        self.window = int(sec("INITIATIVE_RESPONSE_WINDOW_S") or rules.RESPONSE_WINDOW_S)
        self.awaiting_ask = None     # (채널 ask 로 나간 상황 텍스트, 시각): text/out 이 오면 그게 실제로 한 말

    def expect(self, text, drive, value, trigger, now=None):
        self.pending_label[text] = (drive, value, trigger, time.time() if now is None else now)

    def on_bus(self, topic, payload, now=None):
        now = time.time() if now is None else now
        text = payload.decode("utf-8", "replace").strip() if isinstance(payload, bytes) else str(payload).strip()
        if topic == T_SAY:
            self._open("say", text, now)
        elif topic == T_ASK:
            if text.startswith("[시스템"):          # 플릿 워처의 ask (FLEET_REPORT=ask 일 때)
                self._open("ask", text, now, source="fleet", drive="fleet")
            else:
                self._open("ask", text, now)
        elif topic == T_TXT_IN:
            if text.startswith("[ask]"):
                return
            self.settle(text, now)

    def _open(self, channel, text, now, source=None, drive=None):
        label = self.pending_label.pop(text, None)
        if label and now - label[3] < 10:
            drive, value, trigger = label[0], label[1], label[2]
            source = SOURCE
        else:
            value, trigger = None, None
            if source is None:
                fleet = any(k in text for k in ("방금 꺼졌어", "다시 켜졌어", "소식이 없어"))
                source, drive = ("fleet", "fleet") if fleet else ("manual", "unknown")
        aid = selfdb.open_ask(drive, channel, text, source, trigger=trigger, drive_value=value, ts=now)
        print(f"[{time.strftime('%H:%M:%S')}] 장부 열림 #{aid} {source}/{drive} ({channel}): {text[:50]!r}")
        if source == SOURCE:
            selfdb.policy_update(drive, last_ask_ts=int(now))

    def settle(self, text, now):
        """사람이 말했다: 창 안의 가장 최근 ask 를 분류로 닫고, 그 앞의 것은 무응답으로 닫는다."""
        open_ = [a for a in selfdb.open_asks() if now - a["ts"] <= self.window]
        for i, a in enumerate(open_):
            last = i == len(open_) - 1
            cls = rules.classify(text) if last else "unanswered"
            selfdb.close_ask(a["id"], cls, response_text=text if last else None, response_ts=now)
            print(f"[{time.strftime('%H:%M:%S')}] 장부 닫힘 #{a['id']} → {cls}" + (f" ({text[:30]!r})" if last else ""))
            if a["source"] == SOURCE and a["drive"] in DRIVES:
                self._apply(a, cls, now)

    def expire(self, now=None):
        now = time.time() if now is None else now
        for a in selfdb.open_asks():
            if now - a["ts"] > self.window:
                selfdb.close_ask(a["id"], "unanswered", response_ts=a["ts"] + self.window)
                print(f"[{time.strftime('%H:%M:%S')}] 장부 닫힘 #{a['id']} → unanswered (창 지남)")
                if a["source"] == SOURCE and a["drive"] in DRIVES:
                    self._apply(a, "unanswered", now)

    def _apply(self, ask, cls, now):
        pol = selfdb.policy_get(ask["drive"], DRIVES[ask["drive"]]["base_interval_s"])
        repairs = sum(1 for r in selfdb.recent_asks(3, drive=ask["drive"]) if r["repair_strategy"])
        change = rules.apply_outcome(pol, cls, time.localtime(ask["ts"]).tm_hour, repairs_in_a_row=repairs, now=now)
        if change:
            selfdb.policy_update(ask["drive"], **change)
            print(f"[{time.strftime('%H:%M:%S')}] 정책 {ask['drive']}: {change}")
            if ask["repair_strategy"]:
                selfdb.set_repair(ask["id"], ask["repair_strategy"], outcome=cls)

# ---------- 말하기 ----------
def compose(name, info, now):
    """문장과 복구 접두. 직전 같은 추동의 결과가 거절이었으면 설명 또는 약속을 앞에 붙인다."""
    prev = selfdb.recent_asks(1, drive=name)
    repair = None
    if prev and rules.needs_repair(prev[0]["response_class"]):
        pol = selfdb.policy_get(name, DRIVES[name]["base_interval_s"])
        promise = rules.promise_for({"backoff_level": pol["backoff_level"], "avoid_hours": rules.avoid_hours(pol) or None,
                                     "muted_until": pol["muted_until"]})
        if prev[0]["repair_strategy"] != "explain":
            repair = ("explain", rules.repair_line("explain", trigger=prev[0]["trigger"] or info["trigger"],
                                                   asked_ts=prev[0]["ts"], now=now))
        else:
            repair = ("promise", rules.repair_line("promise", promise=promise))
    line = info["line"]
    if repair and DRIVES[name]["channel"] == "say":
        line = repair[1] + " " + line
    elif repair:
        line = line + f" (먼저 이렇게 사과해 줘: {repair[1]})"
    return line, (repair[0] if repair else None)

_held = {}     # drive -> 지난 틱에 말 안 한 이유. 같은 이유가 이어지면 로그를 되풀이하지 않는다 (기기가 꺼진 사흘에 2천 줄이 쌓였다)

def tick(client, body, ledger, hooks, dry_run, now=None):
    now = time.time() if now is None else now
    drives = compute_drives(body, selfdb.last_voice_ts(), now)
    spoke = False
    for name, (value, inputs, info) in drives.items():
        selfdb.record_drive(name, value, inputs, ts=now)
        ok, why = admissible(name, value, body, hooks, now)
        if not ok:
            if value >= DRIVES[name]["threshold"] and _held.get(name) != why:
                print(f"[{time.strftime('%H:%M:%S')}] {name}={value:.2f} 말 안 함: {why} (바뀔 때까지 다시 안 적음)")
            _held[name] = why
            continue
        _held.pop(name, None)
        if spoke:
            continue                       # 한 틱에 하나만
        line, repair = compose(name, info, now)
        channel = DRIVES[name]["channel"]
        plan = {"ts": int(now), "drive": name, "value": round(value, 2), "channel": channel, "text": line, "repair": repair, "dry_run": dry_run}
        client.publish(T_PLAN, json.dumps(plan, ensure_ascii=False), qos=0)
        if dry_run:
            print(f"[{time.strftime('%H:%M:%S')}] (dry-run) {name}={value:.2f} 말했을 것 ({channel}): {line!r}")
            selfdb.note("plan", json.dumps(plan, ensure_ascii=False), ts=now)
            selfdb.policy_update(name, last_ask_ts=int(now))      # dry-run 에서도 간격은 지킨다 (안 그러면 매 틱 떠든다)
            continue
        ledger.expect(line, name, value, info["trigger"], now)
        client.publish(T_SAY if channel == "say" else T_ASK, line, qos=1)
        print(f"[{time.strftime('%H:%M:%S')}] {name}={value:.2f} 말함 ({channel}): {line!r}")
        if repair:
            time.sleep(1)                   # 장부가 먼저 열리게
            for a in selfdb.recent_asks(1, drive=name):
                selfdb.set_repair(a["id"], repair)
        spoke = True
    ledger.expire(now)
    selfdb.link_unlinked_episodes()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="말하지 않고 계획만 기록 (secrets INITIATIVE_DRY_RUN=1 과 같음)")
    ap.add_argument("--once", action="store_true", help="한 틱만 돌고 끝 (상태 토픽이 올 때까지 5초 기다림)")
    ap.add_argument("--db", help="이 DB 파일을 쓴다 (시험용; 기본은 secrets SELF_DB 또는 voice/daijin.db)")
    a = ap.parse_args()
    if a.db:
        selfdb.use(a.db)
    dry_run = a.dry_run or sec("INITIATIVE_DRY_RUN") == "1"
    hooks = load_hooks()
    body, ledger = Body(), Ledger()
    c = cloud_client("daijin-initiative")
    def on_connect(cl, u, f, rc, p):
        print(f"✅ 브로커 연결 → 구독 상태·say·ask·text/in {'(dry-run)' if dry_run else ''}")
        for t, q in ((T_DEV_STATUS, 0), (T_NODE_STATUS, 0), (T_SAY, 1), (T_ASK, 1), (T_TXT_IN, 0), (T_BRAIN_TURN, 0)):
            cl.subscribe(t, qos=q)
    def on_message(cl, u, m):
        if m.topic == T_DEV_STATUS or m.topic.startswith("daijin/dev/"):
            body.note(m.topic, m.payload)
        elif m.topic == T_BRAIN_TURN:
            body.last_brain_turn = time.time()
        else:
            ledger.on_bus(m.topic, m.payload)
    c.on_connect, c.on_message = on_connect, on_message
    print(f"🌱 initiative 시작 — 추동 {list(DRIVES)} · DB {selfdb.db_path()}")
    connect_with_retry(c)
    c.loop_start()
    time.sleep(5)                           # retained 상태가 들어올 시간
    try:
        while True:
            try:
                tick(c, body, ledger, hooks, dry_run)
            except Exception as e:
                print(f"  ⚠️ tick 오류: {e!r}")
            if a.once:
                break
            time.sleep(TICK_S)
    except KeyboardInterrupt:
        pass
    c.loop_stop(); c.disconnect()

if __name__ == "__main__":
    main()
