#!/usr/bin/env python3
# daijin 공통 조각: secrets 읽기, 로컬 확장 로더, 클라우드 브로커 접속.
# 브레인(daijin_mqtt.py), 플릿 워처(fleet_watch.py), 도구 스크립트들이 같이 쓴다. 표준 라이브러리 + paho 만.
import os, ssl, time

VOICE = os.path.dirname(os.path.abspath(__file__))
REPO  = os.path.dirname(VOICE)
SEC   = os.path.join(REPO, "secrets.local.txt")     # 저장소 루트 (클론 위치 무관, gitignore)

def sec(key):
    """secrets.local.txt 의 KEY=value 한 줄. 없으면 "". 매번 파일을 다시 읽으므로 값을 바꿔도 재시작이 필요 없다."""
    for line in open(SEC):
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""

def load_hooks(announce=False):
    """voice/local_hooks.py 가 있으면 불러온다. 저장소에 넣지 않는 개인 파일(.gitignore)로, 이 설치본만의 동작을 얹는 자리다.
    없거나 불러오다 실패하면 None (없는 셈 치고 계속). 정의할 수 있는 것은 전부 선택:
      STT_PROMPT_EXTRA = "..."             STT 어휘 프롬프트 앞에 붙일 문장 (자주 쓰는 말, 고유명사)
      intercept(text, kind, ctx) -> bool    True 를 돌려주면 에이전트를 거치지 않고 거기서 처리 끝 (단축 응답, 빠른 명령).
                                            kind: "voice"(말로 들어온 것) | "ask"(daijin/ask 로 들어온 것)
                                            ctx.say(문장) 으로 말한다. ctx.voice_dir, ctx.t0, ctx.sec(키) 도 쓸 수 있다.
      mute_reports() -> bool                True 면 fleet_watch 가 능동 보고를 쉰다"""
    path = os.path.join(VOICE, "local_hooks.py")
    if not os.path.exists(path):
        return None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("daijin_local_hooks", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if announce:
            print("🔌 로컬 확장 로드: local_hooks.py")
        return mod
    except Exception as e:
        print(f"  ⚠️ local_hooks.py 로드 실패 (없는 셈 치고 계속): {e}")
        return None

def cloud_client(client_id):
    """클라우드 브로커(HiveMQ)용 paho 클라이언트: 자격증명, TLS(시스템 CA로 검증), 끊기면 1~60초 간격 자동 재접속."""
    import paho.mqtt.client as mqtt
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    c.username_pw_set(sec("MQTT_CLOUD_USER"), sec("MQTT_CLOUD_PASS"))
    c.tls_set(cert_reqs=ssl.CERT_REQUIRED)
    c.reconnect_delay_set(min_delay=1, max_delay=60)
    return c

def connect_with_retry(client, retry_s=10):
    """첫 접속이 될 때까지 되풀이한다. 부팅 직후엔 네트워크가 아직 없을 수 있다 (launchd·systemd 로 띄울 때).
    접속 뒤의 끊김은 paho 가 알아서 다시 붙는다."""
    while True:
        try:
            client.connect(sec("MQTT_CLOUD_HOST"), int(sec("MQTT_CLOUD_PORT")), 60)
            return
        except Exception as e:
            print(f"  초기 연결 실패({e}) — {retry_s}초 후 재시도")
            time.sleep(retry_s)
