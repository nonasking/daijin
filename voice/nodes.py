#!/usr/bin/env python3
# 홈 노드 정의의 단일 출처: voice/nodes.json. 노드를 늘리거나 이름을 바꿀 때 고칠 곳은 그 파일 하나다.
#   "<id>": {"n": 번호, "name": 부르는 이름, "aliases": [다른 표기], "color": 대기 LED 색, "flash": 펌웨어 빌드 플래그}
# id 는 MQTT 토픽에 쓰는 이름(dev1), n 은 브레인이 글로 다루는 내부 번호("1번"), name 은 말로 주고받는 이름(레드).
# 이름을 색으로 지은 이유: "일 번/이 번"은 받침 하나 차이라 소음 속에서 뒤집혔고(4/16), 레드/블루는 0/16 (2026-09-22 실측).
#
# 명령줄:  python3 nodes.py flash dev1   → 그 노드의 펌웨어 빌드 플래그 (flash-node.sh 가 쓴다)
import json, os, sys

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nodes.json")

def load(path=_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)

NODES = load()

def by_number(n):
    for nid, d in NODES.items():
        if d["n"] == int(n):
            return nid
    return None

def resolve(word):
    """사람이나 에이전트가 쓴 표기 → 노드 id. 레드, red, 1, 1번, dev1 을 모두 받는다. 모르는 표기는 그대로 돌려준다."""
    w = word.strip()
    if w in NODES or w == "all":
        return w
    low = w.lower()
    for nid, d in NODES.items():
        if low == d["name"].lower() or low in (a.lower() for a in d.get("aliases", [])):
            return nid
        if w in (str(d["n"]), f'{d["n"]}번'):
            return nid
    return w

def spoken(nid):
    """노드 id → 말로 부르는 이름. 표에 없는 id 는 그대로."""
    return NODES[nid]["name"] if nid in NODES else nid

def subject(nid):
    """말로 부르는 이름 + 주격 조사: 레드가, 블루가, 그린이. 받침이 있으면 '이', 없으면 '가'."""
    name = spoken(nid)
    c = name[-1]
    has_batchim = "가" <= c <= "힣" and (ord(c) - 0xAC00) % 28 != 0
    return name + ("이" if has_batchim else "가")

def flash_flags(nid):
    d = NODES[nid]
    return f'{d["flash"]} -DNODE_COLOR="{d["color"]}"'

if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "flash" and sys.argv[2] in NODES:
        print(flash_flags(sys.argv[2]))
    elif len(sys.argv) == 2 and sys.argv[1] == "ids":
        print(" ".join(NODES))
    else:
        sys.exit(f"사용법: nodes.py flash <{'|'.join(NODES)}>  |  nodes.py ids")
