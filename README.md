# ESP32-S3 IoT · daijin 🤖☁️

ESP32-S3(Freenove FNK0082)로 IoT를 배우며, **daijin — AI 음성 대화 친구 IoT 디바이스**를 만들어가는 기록.
디바이스에 말을 걸면 → 클라우드를 거쳐 AI(Claude)가 → 디바이스가 음성으로 답한다.

> 입문자(웹 프론트엔드 개발자)가 케이블 삽질부터 클라우드 음성 비서까지 단계적으로 쌓은 프로젝트.

## 🎯 daijin 아키텍처 (이동형 — 디바이스를 들고 나가도 동작)
```
[ESP32 디바이스: 마이크/스피커]  ──audio──►  HiveMQ 클라우드(MQTT/TLS)  ◄──audio──►  [맥 브레인]
       어느 WiFi에 있든                         (양쪽 outbound 랑데부)        whisper(STT) → Claude → TTS(Yuna)
```
- **클라우드 브로커(HiveMQ)에서 만남** → 디바이스가 외부 망에 있어도 동작. 맥은 집에서 outbound만(노출 불필요).
- 두뇌 = **Claude**(`claude -p`, 에이전트라 집의 LED 같은 것도 음성으로 제어).

## ✅ 현재 상태
- **LED 원격제어**: HTTP(LAN) → MQTT(LAN) → **HiveMQ 클라우드(이동형)** 로 진화. 폰 LTE에서 집 LED 제어 검증 완료.
- **daijin 브레인**: 음성 → STT → Claude → TTS → 음성 왕복, 클라우드 경유 검증 완료(지연 ~7초).
- **다음(Phase 2)**: ESP32에 INMP441 마이크 달아 디바이스가 직접 듣고/말하기. → [docs/chunking-protocol.md](docs/chunking-protocol.md)

## 📁 구조
```
sketches/      ESP32 펌웨어 (Arduino)
  01-blink … 06-dht      학습용 (LED/WiFi/서보/센서)
  10-remote-led          HTTP 원격 LED (초기)
  11-mqtt-led            LAN MQTT LED
  12*-funnel             Tailscale Funnel 시도(이동형엔 불안정 — 기록용)
  13-cloud-led           HiveMQ 클라우드 LED (이동형 ✅)
voice/         daijin (음성 비서)
  daijin_mqtt.py         브레인: MQTT 클라이언트 (STT→Claude→TTS)
  talk.sh                맥-only 음성 루프 (0단계)
  led.sh                 음성 에이전트용 LED 제어(클라우드)
  test_device.py         가짜 디바이스(왕복 테스트)
docs/          학습 로드맵·리서치·프로토콜
```

## 🔧 스택
ESP32-S3 · Arduino(arduino-cli) · MQTT(Mosquitto/HiveMQ Cloud) · TLS(Let's Encrypt) ·
whisper.cpp(STT, 한국어) · Claude(두뇌) · macOS `say`(TTS) · Tailscale · WiFiManager

## ⚙️ 셋업 메모
- **자격증명은 코드에 없음.** `secrets.h`(스케치별)·`secrets.local.txt`·whisper 모델은 **gitignore**. 본인 값으로 직접 생성.
- whisper 모델: `ggml-large-v3-turbo-q5_0.bin`을 `voice/models/`에 직접 다운로드.

## 📌 핵심 교훈
- ESP32는 **2.4GHz WiFi만**, USB는 **데이터 케이블** 필수.
- ESP32-S3 TLS는 **NTP 시간동기화** 필요(타임아웃 걸 것).
- 이동형 백엔드는 **집 맥+터널(불안정)** 보다 **클라우드 브로커**가 정답.

---
*Built with [Claude Code](https://claude.com/claude-code).*
