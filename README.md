# daijin 🤖☁️

> **외로운 개발자의 친구이자 비서.**
> 내가 만들고, 내 시스템에 연결하고, *무엇이든* 시킬 수 있는 AI 음성 대화 IoT 디바이스.

디바이스에 말을 걸면 → 클라우드를 거쳐 AI(**Claude**)가 생각하고 → 디바이스가 음성으로 답한다.
단순히 대답만 하는 게 아니라, **내 집과 내 컴퓨터를 실제로 조종한다.**

ESP32-S3(Freenove FNK0082) + 맥 두뇌 + 클라우드 MQTT로, 케이블 한 가닥부터 차근히 쌓아 만들었다.

## Siri랑 뭐가 달라?
daijin은 "더 나은 Siri"가 아니다. **다른 종류의 물건**이다.

- **두뇌가 명령 매칭이 아니라 추론·행동하는 에이전트.** Siri는 정해진 명령을 맞추는 기계. daijin의 두뇌는 Claude — 실제로 대화하고, `bash`로 내 스크립트·MQTT 기기·데이터에 **행동**한다.
- **내가 소유하고 개조한다.** 성격·프롬프트·할 수 있는 행동을 내가 정의. 닫힌 어시스턴트(Siri·Alexa·심지어 ChatGPT 음성)는 못 주는 — **"내 인프라 안에서 실제로 일하는 에이전트."**
- **폰이 아닌 별개의 존재.** 놓아두고, 들고 다니는 친구. 도구가 아니라 *관계*.

→ daijin의 차별점은 "음성비서"가 아니라 **소유권 + 행위주체성(agency)**. 내가 완전히 소유하고, 내 삶의 시스템에 박혀서, 내가 시키는 무엇이든 실제로 하는 개방형 에이전트 기기.

## 아키텍처 (이동형 — 들고 나가도 동작)
```
[ESP32 디바이스: 마이크/스피커]  ──audio──►  HiveMQ 클라우드(MQTT/TLS)  ◄──audio──►  [맥 두뇌]
       어느 WiFi에 있든                         (양쪽 outbound 랑데부)        whisper(STT) → Claude → TTS
```
- **클라우드 브로커에서 만남** → 디바이스가 외부 망에 있어도 동작. 맥은 집에서 outbound만(외부 노출 불필요).
- 두뇌 = `claude -p` (에이전트라 집의 LED·기기를 음성으로 제어).

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
