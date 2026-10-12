#!/usr/bin/env python3
# 홈 노드·디바이스 조작 도구. 브레인(에이전트)과 사람이 dev.sh / fleet.sh / status.sh / say.sh 로 부른다.
#   nodectl.py cmd <노드|all> <명령>   노드에 명령을 보내고 이번 명령의 ack 를 확인 (dev.sh)
#   nodectl.py fleet                   모든 노드의 온라인 여부 (fleet.sh)
#   nodectl.py status                  메인 디바이스 상태 JSON (status.sh)
#   nodectl.py say [-a] <텍스트>       다이진이 말하게 한다. -a 는 에이전트가 표현을 다듬어 말함 (say.sh)
#
# 브로커 접속 하나로 구독과 발행을 다 한다. 예전엔 mosquitto_sub/pub 을 따로 띄우고 구독이 성립하길
# sleep 으로 기다렸다(클라우드 직결 1.5초 고정). 여기선 SUBACK 을 받은 뒤 바로 발행하므로 기다림이 없다.
# 로컬 mosquitto(1883, 클라우드와 브리지)가 떠 있으면 그쪽으로, 없으면 클라우드로 붙는다.
#
# ACK 방식: state 토픽은 retained 라 그냥 읽으면 "직전" 명령의 ACK 가 먼저 튀어나온다(pong 오답의 원인).
# 그래서 구독을 먼저 열고, retain 표시가 없는(=지금 막 발행된) 메시지만 이번 명령의 ACK 로 친다.
import json, os, queue, socket, sys, threading, time
import paho.mqtt.client as mqtt
from daijin_common import sec, cloud_client, broker_address, broker
from nodes import resolve
import fleet_status

LOCAL = ("127.0.0.1", 1883)

def local_up():
    """집 우체국을 쓸까. BROKER=local (비상 모드) 일 때만.
    예전엔 집 mosquitto 가 떠 있기만 하면 그쪽을 먼저 봤는데, 노드가 집 우체국에 붙었다 클라우드로 돌아가면
    집 쪽에 '오프라인' 유언이 retained 로 남고, 브리지로 오는 하트비트는 retained 가 아니라 덮어쓰지 못한다.
    2026-10-12 실측: 레드가 클라우드에서 멀쩡한데 맥의 dev.sh 가 "오프라인 — 명령 안 보냄"."""
    if broker() != "local":
        return False
    try:
        socket.create_connection(LOCAL, timeout=0.3).close()
        return True
    except OSError:
        return False

class Broker:
    """동기식으로 쓰는 얇은 래퍼: 접속 → 구독(SUBACK 까지) → 발행 → 조건에 맞는 메시지 기다리기."""
    def __init__(self):
        self.local = local_up()
        if self.local:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            c.username_pw_set(sec("MQTT_USER"), sec("MQTT_PASS"))
            host, port = LOCAL
        else:
            c = cloud_client("")                      # 빈 id = 브로커가 임시 id 를 준다 (동시에 여러 번 불러도 충돌 없음)
            host, port = broker_address()
        self.c, self.msgs = c, queue.Queue()
        self._connected, self._subscribed = threading.Event(), threading.Event()
        c.on_connect = lambda *a: self._connected.set()
        c.on_subscribe = lambda *a: self._subscribed.set()
        c.on_message = lambda cl, u, m: self.msgs.put(m)
        c.connect(host, port, 30)
        c.loop_start()
        if not self._connected.wait(10):
            raise ConnectionError("브로커 접속 시간 초과")

    def subscribe(self, *topics):
        self._subscribed.clear()
        self.c.subscribe([(t, 0) for t in topics])
        if not self._subscribed.wait(10):
            raise ConnectionError("구독 응답 없음")

    def publish(self, topic, payload):
        info = self.c.publish(topic, payload, qos=1)   # QoS1: 브로커가 받았는지까지 확인
        info.wait_for_publish(10)
        return info.is_published()

    def wait(self, want, timeout):
        """want(msg) 가 참인 첫 메시지. 시간 안에 없으면 None. 다른 메시지는 버린다."""
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return None
            try:
                m = self.msgs.get(timeout=left)
            except queue.Empty:
                return None
            if want(m):
                return m

    def collect(self, quiet, limit):
        """메시지가 quiet 초 동안 더 안 올 때까지(최대 limit 초) 모은다. retained 묶음을 받는 용도."""
        out, end = [], time.monotonic() + limit
        while time.monotonic() < end:
            try:
                out.append(self.msgs.get(timeout=quiet if out else max(0.0, end - time.monotonic())))
            except queue.Empty:
                break
        return out

    def close(self):
        self.c.disconnect()        # 먼저 끊어야 네트워크 루프가 바로 깨어난다 (순서를 바꾸면 loop_stop 이 1초 가까이 기다린다)
        self.c.loop_stop()

def text(m):
    return m.payload.decode("utf-8", "replace")

def cmd(node_word, command):
    node = resolve(node_word)
    b = Broker()
    try:
        if node == "all":
            if b.publish("daijin/dev/all/cmd", command):
                print(f"OK: 전 노드 <- {command}"); return 0
            print("FAIL: 브로커 전송 실패"); return 1
        t_status, t_state, t_cmd = (f"daijin/dev/{node}/{x}" for x in ("status", "state", "cmd"))
        b.subscribe(t_status, t_state)
        # 오프라인 노드는 ack 를 14초 기다리지 말고 retained status 로 바로 판정한다 (구독 직후에 온다)
        st = b.wait(lambda m: m.topic == t_status, 0.4 if b.local else 1.2)
        if st is not None and '"online":false' in text(st).replace(" ", ""):
            print(f"FAIL: {node} 오프라인 (전원 또는 네트워크 끊김) — 명령 안 보냄"); return 1
        if st is None:
            print(f"참고: {node} 상태 기록 없음 (한 번도 접속한 적 없는 이름?) — 일단 보내봄")
        fresh = lambda m: m.topic == t_state and not m.retain
        if not b.publish(t_cmd, command):
            print("FAIL: 브로커 전송 실패"); return 1
        # 동작(step:360 은 5초, poke/alarm 최대 3초) + 브로커 왕복까지 넉넉히 14초
        m = b.wait(fresh, 14)
        ack = text(m) if m else ""
        print(f"OK: {node} <- {command} (ack: {ack or '없음: 노드가 오프라인이거나 응답 지연'})")
        if ack.startswith("servo:") and ack.endswith(":nochange"):
            print("참고: 서보가 이미 그 각도라 움직임이 안 보였을 거야. 눈에 보이게 돌리려면 'motor' (갔다가 돌아옴)를 써.")
            return 0
        if ack.startswith("err:unknown:"):
            # 모르는 명령이면 그 자리에서 기능 목록을 읽어 같이 보여준다 (브레인이 따로 조회할 필요 없게)
            b.publish(t_cmd, "caps")
            c = b.wait(fresh, 6)
            caps = text(c)[len("caps:"):] if c and text(c).startswith("caps:") else "?"
            print(f"이 노드({node})에 없는 명령. 쓸 수 있는 기능: {caps}  (모터를 돌리려면 노드 종류와 상관없이 motor 를 써)")
            return 1
        return 1 if (not ack or ack.startswith("err:")) else 0
    finally:
        b.close()

def fleet():
    b = Broker()
    try:
        b.subscribe("daijin/dev/+/status")
        msgs = b.collect(quiet=0.15 if b.local else 0.5, limit=1.0 if b.local else 3.0)
    finally:
        b.close()
    if not msgs:
        print("노드 없음 (retained status가 하나도 없음 — 아직 아무 노드도 접속한 적 없음)"); return 1
    print("\n".join(fleet_status.render([(m.topic, text(m)) for m in msgs])))
    return 0

def status():
    b = Broker()
    try:
        b.subscribe("daijin/status")
        m = b.wait(lambda m: m.topic == "daijin/status", 5)
    finally:
        b.close()
    if m is None:
        print("FAIL: 디바이스 상태 없음 (디바이스가 꺼져 있거나 아직 미보고)"); return 1
    print(device_status_line(text(m)))
    return 1 if '"online":false' in text(m).replace(" ", "") else 0

def device_status_line(payload, now=None):
    """디바이스 status JSON → 출력할 한 줄. 꺼져 있으면(LWT) FAIL 문구, 켜져 있으면 JSON 에 age_s(몇 초 전 값인지)를 더한다.
    online/ts 가 없는 예전 펌웨어의 JSON 은 그대로 내보낸다."""
    try:
        d = json.loads(payload)
    except ValueError:
        return payload
    if d.get("online") is False:
        return "FAIL: 디바이스 오프라인 (전원 또는 네트워크 끊김)"
    ts = d.get("ts", 0)
    if ts and ts > 1700000000:
        d["age_s"] = int((time.time() if now is None else now) - ts)
    return json.dumps(d, ensure_ascii=False)

def say(args):
    topic = "daijin/say"
    if args and args[0] == "-a":
        topic, args = "daijin/ask", args[1:]
    if not args:
        print("말할 텍스트 필요"); return 2
    b = Broker()
    try:
        ok = b.publish(topic, args[0])
    finally:
        b.close()
    print(f"OK: {topic} <- {args[0]}" if ok else "FAIL: 브로커 전송 실패")
    return 0 if ok else 1

def main(argv):
    try:
        if len(argv) == 3 and argv[0] == "cmd":
            return cmd(argv[1], argv[2])
        if argv == ["fleet"]:
            return fleet()
        if argv == ["status"]:
            return status()
        if argv and argv[0] == "say":
            return say(argv[1:])
    except (ConnectionError, OSError) as e:
        print(f"FAIL: 브로커 접속 실패 ({e})"); return 1
    print("사용법: nodectl.py cmd <노드|all> <명령> | fleet | status | say [-a] <텍스트>")
    return 2

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
