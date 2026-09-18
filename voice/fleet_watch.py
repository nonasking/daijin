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
T_MAIN   = "daijin/status"      # 메인 디바이스 상태 (status.sh용)
# 로컬 브로커 미러: 브리지는 라이브 메시지의 retain 플래그를 못 살려서(MQTT 규격) 로컬 retained가
# 낡은 값으로 남는다. 그래서 클라우드에서 받은 상태를 로컬(1883)에 retained로 다시 써준다.
# fleet.sh/status.sh/dev.sh가 로컬에서 즉시 최신 상태를 읽을 수 있는 근거.
LOCAL_HOST, LOCAL_PORT = "localhost", 1883
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

local = None   # 로컬 미러 클라이언트 (연결 실패 시 None → 미러 생략)

def mirror(topic, payload):
    if local is not None and local.is_connected():
        try:
            local.publish(topic, payload, qos=0, retain=True)
        except Exception as e:
            print(f"(로컬 미러 실패: {e})")

def on_message(client, userdata, msg):
    mirror(msg.topic, msg.payload)
    if not msg.topic.startswith("daijin/dev/"):
        return
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
    client.subscribe(T_MAIN, qos=0)

c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="daijin-fleetwatch")
c.username_pw_set(sec("MQTT_CLOUD_USER"), sec("MQTT_CLOUD_PASS"))
c.tls_set(cert_reqs=ssl.CERT_REQUIRED)
c.reconnect_delay_set(min_delay=1, max_delay=60)
c.on_connect = on_connect
c.on_message = on_message

print("👁️ fleet_watch 시작")
# 로컬 미러 — 부팅 직후엔 mosquitto보다 먼저 뜰 수 있어서(2026-09-17 실측: Connection refused 후 미러 영구 생략)
# connect_async + loop_start로 붙을 때까지 paho가 알아서 재시도한다. 끊겨도 자동 재접속.
_l = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="daijin-mirror")
_l.username_pw_set(sec("MQTT_USER"), sec("MQTT_PASS"))
_l.reconnect_delay_set(min_delay=1, max_delay=30)
_l.on_connect = lambda c, u, f, rc, p=None: print(f"🪞 로컬 브로커 미러 ON (localhost:1883, rc={rc})")
_l.on_disconnect = lambda c, u, f, rc, p=None: print(f"🪞 로컬 브로커 끊김(rc={rc}) → 재시도")
_l.connect_async(LOCAL_HOST, LOCAL_PORT, 60); _l.loop_start(); local = _l
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
