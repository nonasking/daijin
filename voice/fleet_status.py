#!/usr/bin/env python3
# 플릿 상태 판정 — 노드별 retained status(하트비트/LWT) JSON 을 사람이 읽을 한 줄로.
#   online=false                         → OFFLINE (브로커 LWT 가 남긴 것)
#   online=true 인데 ts 가 STALE_S 초과   → STALE   (하트비트 중단 의심)
#   그 외                                → ONLINE
# nodectl.py fleet 이 쓴다. 파이프로도 된다: mosquitto_sub -v -t 'daijin/dev/+/status' | python3 fleet_status.py
import sys, json, time
from nodes import spoken

STALE_S = 90     # 하트비트 주기는 30초

def describe(node, payload, now=None):
    """노드 id 와 status 페이로드(문자열) → 한 줄."""
    now = time.time() if now is None else now
    name = spoken(node)
    try:
        d = json.loads(payload)
    except Exception:
        return f"{name}: 파싱 불가: {payload}"
    if not d.get("online", False):
        return f"{name}: OFFLINE (브로커 LWT — 전원 또는 네트워크 끊김)"
    ts = d.get("ts", 0)
    age = int(now - ts) if ts and ts > 1700000000 else None
    state = "ONLINE" if age is None or age < STALE_S else f"STALE({age}초 무응답)"
    extra = ""
    if "room_c" in d and d["room_c"] != -1:
        extra = f' 실내 {d["room_c"]}°C/{d.get("room_rh", "?")}%'
    tail = f" · 하트비트 {age}초 전" if age is not None else " · 시간 미동기"
    if d.get("ssid"):
        tail += f' · 망 {d["ssid"]}'
    return (f'{name}: {state} · 칩 {d.get("chip_c", "?")}°C · RSSI {d.get("rssi", "?")}'
            f' · 업타임 {d.get("up_s", "?")}초{tail}{extra}')

def render(messages, now=None):
    """[(topic, payload)] → 줄 목록. 같은 노드의 메시지가 여럿이면(retained + 미러 사본) ts 가 가장 큰 것 하나만 쓴다."""
    latest = {}
    for topic, payload in messages:
        try:
            ts = json.loads(payload).get("ts", 0)
        except Exception:
            ts = 0
        if topic not in latest or ts >= latest[topic][0]:
            latest[topic] = (ts, payload)
    lines = []
    for topic in sorted(latest):
        node = topic.split("/")[2] if topic.count("/") >= 2 else topic
        lines.append(describe(node, latest[topic][1], now))
    return lines

if __name__ == "__main__":
    msgs = [tuple(l.strip().split(" ", 1)) for l in sys.stdin if " " in l.strip()]
    print("\n".join(render(msgs)))
