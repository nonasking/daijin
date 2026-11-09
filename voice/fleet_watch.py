#!/usr/bin/env python3
# fleet_watch — 홈 노드 장애 워처: 상태 전이를 감지해 다이진이 먼저 말하게 한다.
#   OFFLINE 전이(브로커 LWT)  → 정해진 문장을 daijin/say 로 바로 말한다 (에이전트를 거치지 않아 지연 0, 세션 토큰 절약)
#   ONLINE 복귀               → 같은 경로로 복귀 보고 (업타임이 짧으면 "재부팅한 것 같아"를 덧붙인다)
#   STALE(online인데 하트비트 90초+ 끊김) → 1회 보고
#   secrets 의 FLEET_REPORT=ask 면 예전처럼 상황을 daijin/ask 로 넘겨 에이전트가 표현을 다듬는다 (6초쯤 느리다).
# 시작 시의 retained 상태는 베이스라인으로만 삼고 알리지 않는다 (재시작 때마다 떠들지 않게).
# 노드별 쿨다운 60초 — 전원 갈아끼우는 작업 중 스팸 방지.
# 실행: python3 -u fleet_watch.py  (상시 구동은 systemd daijin-fleetwatch, 맥이면 LaunchAgent com.daijin.fleetwatch)
# 예전엔 받은 상태를 맥의 로컬 브로커에 retained 로 다시 써 주는 '미러'가 있었다 (헬퍼 스크립트가 localhost 에서 읽던 시절).
# 헬퍼가 우체국에 직접 붙고 맥의 다리도 걷은 뒤로 읽는 곳이 없어 뺐다 (2026-11-09).

import json, time
from daijin_common import sec, load_hooks, cloud_client, connect_with_retry
from nodes import NODES, subject

T_STATUS = "daijin/dev/+/status"
T_MAIN   = "daijin/status"      # 메인 디바이스 상태 (status.sh용)
T_ASK    = "daijin/ask"
T_SAY    = "daijin/say"
STALE_S    = 90     # 하트비트(30초 주기)가 이 시간 이상 끊기면 STALE
COOLDOWN_S = 60     # 노드별 최소 보고 간격
BASELINE_S = 3      # 시작 후 이 시간 동안의 retained는 베이스라인 (무보고)
OFF_GRACE_S = 90    # 꺼짐은 이만큼 기다렸다 말한다. 그 안에 다시 켜지면 둘 다 말하지 않는다 (재부팅·플래시·케이블 바꾸기).
                    # 2026-10-12 실측: 보드를 꽂았다 뺐다 하는 하루 동안 "꺼졌어/켜졌어"를 27번 말했다.

started = time.time()
nodes = {}   # name -> {"online": bool, "ts": float(수신시각), "beat": int(하트비트 ts), "alerted_at": float, "stale_alerted": bool}

def speak(client, line, context):
    """보고 한 줄. 기본은 그대로 말하기(daijin/say). FLEET_REPORT=ask 면 상황 설명(context)을 에이전트에 넘긴다."""
    if sec("FLEET_REPORT") == "ask":
        client.publish(T_ASK, context)
        print(f"[{time.strftime('%H:%M:%S')}] ask → {context}")
    else:
        client.publish(T_SAY, line)
        print(f"[{time.strftime('%H:%M:%S')}] say → {line}")

def lines_offline(node):
    return (f"{subject(node)} 방금 꺼졌어. 전원이나 네트워크를 확인해 줘.",
            f"[시스템 알림] 홈 노드 {node}가 방금 오프라인이 됐어(브로커 LWT). "
            f"필요하면 fleet.sh로 전체 상태를 확인하고, 원인 추측과 함께 짧게 보고해줘.")

def lines_online(node, up_s):
    try:
        rebooted = int(up_s) < 120
    except (TypeError, ValueError):
        rebooted = False
    return (f"{subject(node)} 다시 켜졌어." + (" 방금 재부팅한 것 같아." if rebooted else ""),
            f"[시스템 알림] 홈 노드 {node}가 다시 온라인이 됐어(업타임 {up_s}초). "
            f"복구 소식을 짧게 알려줘. 업타임이 짧으면 재부팅했다는 뜻이야.")

def lines_stale(node, age):
    return (f"{subject(node)} 연결은 돼 있는데 {int(age)}초째 소식이 없어. 멈춘 것 같아.",
            f"[시스템 알림] 홈 노드 {node}가 온라인으로 보이는데 하트비트가 {int(age)}초째 없어. "
            f"연결은 살았는데 펌웨어가 멈췄을 수도 있어. 짧게 알려줘.")

HOOKS = load_hooks()      # voice/local_hooks.py (선택). mute_reports() 가 True 를 돌려주면 능동 보고를 쉰다

def muted():
    fn = getattr(HOOKS, "mute_reports", None)
    try:
        return bool(fn()) if fn else False
    except Exception as e:
        print(f"(local_hooks.mute_reports 오류: {e})")
        return False

def report(client, node, lines, kind):
    """lines = (그대로 말할 문장, 에이전트용 상황 설명). kind별 쿨다운 — 오프라인 직후의 복귀 보고가 잘리지 않도록
    종류를 분리. 플래핑(널뛰기) 노드도 종류당 분당 1회로 제한된다."""
    line, context = lines
    if muted():
        print(f"[{time.strftime('%H:%M:%S')}] (보고 쉼, local_hooks) {line}")
        return
    st = nodes[node]
    key = f"alerted_{kind}"
    if time.time() - st.get(key, 0) < COOLDOWN_S:
        print(f"[{time.strftime('%H:%M:%S')}] (쿨다운, 생략) {line}")
        return
    st[key] = time.time()
    speak(client, line, context)

def on_message(client, userdata, msg):
    if not msg.topic.startswith("daijin/dev/"):
        return
    node = msg.topic.split("/")[2]
    if node not in NODES:                     # 표에 없는 이름은 버린다 (FLEET_REPORT=ask 면 이 이름이 에이전트에 간다)
        return
    try:
        d = json.loads(msg.payload)
    except ValueError:
        return
    online = bool(d.get("online", False))
    prev = nodes.get(node)
    was_online = prev.get("online") if prev else None   # update로 덮어쓰기 전에 확보
    now = time.time()
    baseline = (now - started) < BASELINE_S or prev is None
    nodes.setdefault(node, {})
    nodes[node].update({"online": online, "ts": now, "stale_alerted": False})
    if online:
        nodes[node]["beat"] = d.get("ts", 0)
        nodes[node]["up_s"] = d.get("up_s", 0)
    if baseline:
        print(f"[{time.strftime('%H:%M:%S')}] 베이스라인 {node}: {'ONLINE' if online else 'OFFLINE'}")
        return
    if was_online and not online:
        nodes[node]["off_since"] = now                    # 바로 말하지 않는다: pending_check 가 OFF_GRACE_S 뒤에 말한다
        print(f"[{time.strftime('%H:%M:%S')}] {node} 꺼짐, {OFF_GRACE_S}초 기다렸다 보고")
    elif (not was_online) and online:
        if nodes[node].pop("off_since", None) is not None:
            print(f"[{time.strftime('%H:%M:%S')}] {node} 잠깐 꺼졌다 다시 켜짐 (업타임 {d.get('up_s', '?')}초), 보고 안 함")
        else:
            report(client, node, lines_online(node, d.get("up_s", "?")), "on")

def pending_check(client, now=None):
    """OFF_GRACE_S 동안 다시 켜지지 않은 노드의 꺼짐을 이제 말한다."""
    now = time.time() if now is None else now
    for node, st in nodes.items():
        since = st.get("off_since")
        if since is not None and not st.get("online") and now - since >= OFF_GRACE_S:
            st.pop("off_since")
            report(client, node, lines_offline(node), "off")

def stale_check(client):
    now = time.time()
    for node, st in nodes.items():
        if not st.get("online") or st.get("stale_alerted"):
            continue
        beat = st.get("beat", 0)
        ref = beat if beat > 1_700_000_000 else 0
        # 하트비트 ts가 유효하면 그것으로, 아니면 마지막 수신 시각으로 판정
        age = (now - ref) if ref else (now - st.get("ts", now))
        if age > STALE_S:
            st["stale_alerted"] = True
            report(client, node, lines_stale(node, age), "stale")

def on_connect(client, userdata, flags, rc, properties):
    print(f"✅ 브로커 연결 → 구독 {T_STATUS}")
    client.subscribe(T_STATUS, qos=1)
    client.subscribe(T_MAIN, qos=0)

def main():
    global started
    started = time.time()
    c = cloud_client("daijin-fleetwatch")
    c.on_connect = on_connect
    c.on_message = on_message
    print("👁️ fleet_watch 시작")
    connect_with_retry(c)
    c.loop_start()
    try:
        while True:
            time.sleep(15)
            pending_check(c)
            stale_check(c)
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
