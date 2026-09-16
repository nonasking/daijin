#!/usr/bin/env python3
# fleet.sh의 파서 — mosquitto_sub -v 출력(토픽 페이로드)을 읽어 노드별 상태를 판정한다.
import sys, json, time

now = time.time()
# 같은 노드의 메시지가 여러 개 오면(retained + 미러 사본) ts가 가장 큰 것 하나만 쓴다
latest = {}
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    topic, _, payload = line.partition(" ")
    try:
        ts = json.loads(payload).get("ts", 0)
    except Exception:
        ts = 0
    if topic not in latest or ts >= latest[topic][0]:
        latest[topic] = (ts, line)
for _, line in sorted(latest.values(), key=lambda x: x[1]):
    line = line.strip()
    if not line:
        continue
    topic, _, payload = line.partition(" ")
    node = topic.split("/")[2] if topic.count("/") >= 2 else topic
    node = {"dev1": "1번", "dev2": "2번"}.get(node, node)
    try:
        d = json.loads(payload)
    except Exception:
        print(f"{node}: 파싱 불가: {payload}")
        continue
    if not d.get("online", False):
        print(f"{node}: OFFLINE (브로커 LWT — 전원 또는 네트워크 끊김)")
        continue
    ts = d.get("ts", 0)
    age = int(now - ts) if ts and ts > 1700000000 else None
    state = "ONLINE" if age is None or age < 90 else f"STALE({age}초 무응답)"
    extra = ""
    if "room_c" in d and d["room_c"] != -1:
        extra = f' 실내 {d["room_c"]}°C/{d.get("room_rh", "?")}%'
    tail = f" · 하트비트 {age}초 전" if age is not None else " · 시간 미동기"
    if d.get("ssid"):
        tail += f' · 망 {d["ssid"]}'
    print(f'{node}: {state} · 칩 {d.get("chip_c", "?")}°C · RSSI {d.get("rssi", "?")}'
          f' · 업타임 {d.get("up_s", "?")}초{tail}{extra}')
