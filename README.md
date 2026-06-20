# daijin

> **외로운 개발자의 친구이자 비서.**
> 내가 만들고, 내 시스템에 연결하고, *무엇이든* 시킬 수 있는 AI 음성 대화 IoT 디바이스.

디바이스에 말을 걸면 → 클라우드를 거쳐 AI(**Claude**)가 생각하고 → 디바이스가 음성으로 답한다.
단순히 대답만 하는 게 아니라, **내 집과 내 컴퓨터를 실제로 조종한다.**

ESP32-S3(Freenove FNK0082) + 맥 두뇌 + 클라우드 MQTT로, 케이블 한 가닥부터 차근히 쌓아 만들었다.

## 아키텍처 (이동형 — 들고 나가도 동작)
```
[ESP32 디바이스: 마이크/스피커]  ──audio──►  HiveMQ 클라우드(MQTT/TLS)  ◄──audio──►  [맥 두뇌]
       어느 WiFi에 있든                         (양쪽 outbound 랑데부)        whisper(STT) → Claude → TTS
```
- **클라우드 브로커에서 만남** → 디바이스가 외부 망에 있어도 동작. 맥은 집에서 outbound만(외부 노출 불필요).
- 두뇌 = `claude -p` (에이전트라 집의 LED·기기를 음성으로 제어).

## 확장 — 다른 에이전트에 붙이기 (설계상 열려 있음)
daijin은 **"귀·입(디바이스)"과 "두뇌(에이전트)"가 MQTT 토픽으로 분리**돼 있다. 그래서 두뇌를 갈아끼울 수 있고, daijin 자체를 *어떤 에이전트의 음성 입출력 장치*로도 쓸 수 있다.

- **두뇌 교체**: `claude -p` 자리에 OpenAI·Gemini·로컬 LLM(EXAONE)·Claude Agent SDK 등 무엇이든. STT/TTS·디바이스는 그대로.
- **범용 음성 I/O**: `daijin/text/in`(내 말)·`daijin/text/out`(답) 토픽만 구독/발행하면, 어떤 에이전트든 daijin을 귀와 입으로 사용. (오디오·STT·TTS는 daijin이 처리)
- **멀티 에이전트 라우팅**: 전사된 의도에 따라 집-제어 에이전트 / 코딩 에이전트 / 잡담 에이전트로 분배.
- **MCP 연동**: daijin의 행동(말하기·기기 제어)을 MCP 도구로 노출하거나, 두뇌가 여러 MCP 서버를 물려 능력을 모듈로 확장.

> 토픽 계약(`daijin/audio/*`, `daijin/text/*`)이 이미 인터페이스 역할을 하므로, 두뇌를 바꾸거나 외부 에이전트를 붙이는 건 *어댑터 한 겹*이면 된다. (현재 두뇌=Claude, 확장은 로드맵.)

## 현재 상태
- **원격 제어**: HTTP(LAN) → MQTT(LAN) → **HiveMQ 클라우드(이동형)** 로 진화. 폰 LTE에서 집 기기 제어 검증 완료.
- **daijin 두뇌**: 음성 → STT → Claude → TTS → 음성 왕복, 클라우드 경유 검증 완료(지연 ~7초).
- **다음(Phase 2)**: ESP32에 INMP441 마이크 달아 디바이스가 직접 듣고/말하기. → [docs/chunking-protocol.md](docs/chunking-protocol.md)

## 구조
```
sketches/   ESP32 펌웨어
  01~06               학습용(LED/WiFi/서보/센서)
  10-remote-led       HTTP 원격 LED (초기)
  11-mqtt-led         LAN MQTT
  12*-funnel          Tailscale Funnel 시도(이동형엔 불안정 — 기록)
  13-cloud-led        HiveMQ 클라우드 (이동형 ✅)
voice/      daijin
  daijin_mqtt.py      두뇌: MQTT 클라이언트 (STT→Claude→TTS)
  talk.sh             맥-only 음성 루프
  led.sh              음성 에이전트용 기기 제어
  test_device.py      가짜 디바이스 왕복 테스트
docs/       로드맵·리서치·프로토콜
```

## 스택
ESP32-S3 · Arduino(arduino-cli) · MQTT(Mosquitto / HiveMQ Cloud) · TLS(Let's Encrypt) ·
whisper.cpp(STT, 한국어) · Claude(두뇌) · TTS · WiFiManager

## 셋업 메모
- **자격증명은 코드에 없음.** `secrets.h`·`secrets.local.txt`·whisper 모델은 gitignore. 본인 값으로 직접 생성.
- whisper 모델 `ggml-large-v3-turbo-q5_0.bin` 은 `voice/models/` 에 직접 다운로드.

## 핵심 교훈
- ESP32는 **2.4GHz WiFi만**, USB는 **데이터 케이블** 필수.
- ESP32-S3 TLS는 **NTP 시간동기화** 필요(타임아웃 걸 것).
- 이동형 백엔드는 **집 맥+터널(불안정)** 보다 **클라우드 브로커**가 정답.

---
*Built with [Claude Code](https://claude.com/claude-code).*
