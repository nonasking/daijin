#!/usr/bin/env python3
# fleet_watch — 홈 노드 장애 워처: 상태 전이를 감지해 다이진이 먼저 말하게 한다.
#   OFFLINE 전이(브로커 LWT)  → daijin/ask 로 상황 전달 → 다이진이 스스로 표현해 능동 보고
#   ONLINE 복귀               → 같은 경로로 복귀 보고
#   STALE(online인데 하트비트 90초+ 끊김) → 1회 보고
# 시작 시의 retained 상태는 베이스라인으로만 삼고 알리지 않는다 (재시작 때마다 떠들지 않게).
# 노드별 쿨다운 60초 — 전원 갈아끼우는 작업 중 스팸 방지.
# 실행: python3 -u fleet_watch.py  (상시 구동은 LaunchAgent com.daijin.fleetwatch)

import paho.mqtt.client as mqtt
import json, os, ssl, time

HOME = os.path.expanduser("~")
SEC  = f"{HOME}/esp32-iot/secrets.local.txt"
T_STATUS = "daijin/dev/+/status"
T_ASK    = "daijin/ask"
STALE_S    = 90     # 하트비트(30초 주기)가 이 시간 이상 끊기면 STALE
COOLDOWN_S = 60     # 노드별 최소 보고 간격
BASELINE_S = 3      # 시작 후 이 시간 동안의 retained는 베이스라인 (무보고)

def sec(key):
    for line in open(SEC):
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""

started = time.time()
nodes = {}   # name -> {"online": bool, "ts": float(수신시각), "beat": int(하트비트 ts), "alerted_at": float, "stale_alerted": bool}

def ask(client, text):
    client.publish(T_ASK, text)
    print(f"[{time.strftime('%H:%M:%S')}] ask → {text}")

def report(client, node, text, kind):
    """kind별 쿨다운 — 오프라인 직후의 복귀 보고가 잘리지 않도록 종류를 분리.
    플래핑(널뛰기) 노드도 종류당 분당 1회로 제한된다."""
    st = nodes[node]
    key = f"alerted_{kind}"
    if time.time() - st.get(key, 0) < COOLDOWN_S:
        print(f"[{time.strftime('%H:%M:%S')}] (쿨다운, 생략) {text}")
        return
    st[key] = time.time()
    ask(client, text)

def on_message(client, userdata, msg):
    node = msg.topic.split("/")[2]
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
        report(client, node,
               f"[시스템 알림] 홈 노드 {node}가 방금 오프라인이 됐어(브로커 LWT). "
               f"필요하면 fleet.sh로 전체 상태를 확인하고, 원인 추측과 함께 짧게 보고해줘.", "off")
    elif (not was_online) and online:
        up = d.get("up_s", "?")
        report(client, node,
               f"[시스템 알림] 홈 노드 {node}가 다시 온라인이 됐어(업타임 {up}초). "
               f"복구 소식을 짧게 알려줘. 업타임이 짧으면 재부팅했다는 뜻이야.", "on")

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
            report(client, node,
                   f"[시스템 알림] 홈 노드 {node}가 온라인으로 보이는데 하트비트가 {int(age)}초째 없어. "
                   f"연결은 살았는데 펌웨어가 멈췄을 수도 있어. 짧게 알려줘.", "stale")

def on_connect(client, userdata, flags, rc, properties):
    print(f"✅ 브로커 연결 → 구독 {T_STATUS}")
    client.subscribe(T_STATUS, qos=1)

c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="daijin-fleetwatch")
c.username_pw_set(sec("MQTT_CLOUD_USER"), sec("MQTT_CLOUD_PASS"))
c.tls_set(cert_reqs=ssl.CERT_REQUIRED)
c.reconnect_delay_set(min_delay=1, max_delay=60)
c.on_connect = on_connect
c.on_message = on_message

print("👁️ fleet_watch 시작")
while True:
    try:
        c.connect(sec("MQTT_CLOUD_HOST"), int(sec("MQTT_CLOUD_PORT")), 60)
        break
    except Exception as e:
        print(f"초기 연결 실패({e}) — 10초 후 재시도")
        time.sleep(10)
c.loop_start()
try:
    while True:
        time.sleep(15)
        stale_check(c)
except KeyboardInterrupt:
    pass
