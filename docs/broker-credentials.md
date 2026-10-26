# 브로커 자격증명 나누기

브레인의 에이전트는 셸을 포함한 도구를 자유롭게 쓴다. 그래서 **브로커에 발행할 수 있는 자격증명이 곧 보안 경계**다.
`daijin/ask` 에 글을 보내거나 `daijin/audio/in` 에 음성을 보낼 수 있으면 에이전트를 움직일 수 있다.

브레인, 디바이스, 홈 노드가 같은 사용자·비밀번호를 쓰면 가장 허술하게 놓인 기기가 전체의 약점이 된다.
기기의 플래시에는 비밀번호가 들어 있고, 그 값 하나로 에이전트까지 닿기 때문이다. 그래서 기기마다 자기 토픽만 쓸 수 있는 자격증명을 따로 준다.

## 나눌 자격증명

| 누가 | 필요한 토픽 | 권한 |
|---|---|---|
| 브레인 (플릿 워처, 헬퍼 스크립트 포함) | `daijin/#` | 발행 + 구독 |
| 디바이스 (14-daijin) | `daijin/audio/out` 구독, `daijin/audio/in`·`daijin/status` 발행 | 아래 참고 |
| 홈 노드 (15-home-node) | `daijin/dev/#` | 발행 + 구독 |

핵심은 **노드 자격증명**이다. `daijin/dev/#` 만 쓸 수 있으면 노드끼리의 명령·상태는 그대로 되고,
`daijin/ask`·`daijin/say`·`daijin/audio/in` 에는 닿지 못한다.

디바이스는 마이크라서 원래 에이전트에게 말을 거는 물건이다. 권한을 좁혀도 `daijin/audio/in` 발행은 남는다.
그래도 `daijin/ask`·`daijin/dev/#` 를 막아 두면 디바이스 비밀번호가 새도 할 수 있는 일이 줄어든다.

## 자체 우체국 (2026-10-25부터)

브레인과 같은 VPS 에 Mosquitto 가 있다 (`daijin.inanna.day:8883`, TLS, Let's Encrypt). 설정은 `voice/mosquitto/`, 설치는
`sudo MQTT_DOMAIN=daijin.inanna.day bash voice/mosquitto/install.sh`. 위 표가 그대로 `voice/mosquitto/daijin.acl` 이고 사용자는
`brain`·`device`·`nodes` 셋이다. 비밀번호는 맥 키체인 `daijin-mqtt-<사용자>` 에 있다.

- 브레인·워처·initiative 는 같은 기계라 `127.0.0.1:1883` 평문으로 붙는다 (secrets 의 `MQTT_CLOUD_HOST=127.0.0.1`, `PORT=1883`)
- 2026-10-26 디바이스·레드·블루 모두 옮겼고 HiveMQ 다리는 걷었다. 맥의 헬퍼 스크립트도 같은 우체국을 본다 (맥 secrets 의 `MQTT_CLOUD_*`)
- 보드 옮기기: `sketches/<스케치>/secrets.h` 에서 `MQTT_HOST "daijin.inanna.day"`, `MQTT_USER`/`MQTT_PASS` 를 그 보드의 사용자로 (디바이스=`device`, 노드=`nodes`)
  바꾸고 플래시. `ca_cert.h` 는 그대로 (같은 ISRG Root X1)
- 되돌리기: `secrets.local.txt.bak-hivemq-*` (VPS·맥) 와 `sketches/*/secrets.h.bak-hivemq` 에 옛 값이 있다. HiveMQ 클러스터는 아직 지우지 않았다

## HiveMQ Cloud 에서 (예전 방식)

1. 클러스터의 **Access Management** 에서 권한(permission)을 만든다. 무료 플랜은 권한 하나에 토픽 필터 하나다
   - `nodes`: 필터 `daijin/dev/#`, publish + subscribe
   - `brain`: 필터 `daijin/#`, publish + subscribe
   - 디바이스용은 콘솔에서 자격증명 하나에 권한을 여러 개 붙일 수 있는지에 따라 정한다. 붙일 수 있으면
     `daijin/audio/#` 와 `daijin/status` 두 개를, 하나만 되면 `daijin/#` 를 쓰고 노드만 좁힌다 (콘솔에서 확인할 것)
2. 자격증명을 셋 만들고 각각 위 권한을 붙인다
3. 값을 넣는 곳
   - 브레인: 저장소 루트 `secrets.local.txt` 의 `MQTT_CLOUD_USER` / `MQTT_CLOUD_PASS`
   - 디바이스: `sketches/14-daijin/secrets.h` 의 `MQTT_USER` / `MQTT_PASS`
   - 노드: `sketches/15-home-node/secrets.h` 의 `MQTT_USER` / `MQTT_PASS`
4. 디바이스와 노드를 다시 플래시하고, 다 붙는 것을 확인한다. 공용 자격증명을 쓰고 있었다면 그 뒤에 **지운다**

코드는 바꿀 것이 없다. 이미 세 곳이 각자의 파일에서 값을 읽는다.

## 같이 볼 것

- 펌웨어는 시계를 못 맞춰도 인증서 검증을 끄지 않는다 (`tls_time.h`). 검증이 꺼지면 같은 망에서 비밀번호를 가로챌 수 있다
- 로컬 mosquitto 브리지를 쓰면 브리지 설정에도 브레인 자격증명이 들어간다
