# ESP32-S3 IoT 학습 로드맵 — 배송 기다리는 동안

> 키트: **Freenove FNK0082 (ESP32-S3-WROOM CAM Ultimate)**
> 보드 식별자(중요): **`esp32-s3-devkitc-1`** (PlatformIO) / FQBN **`esp32:esp32:esp32s3`** (arduino-cli)
> 최종 목표: **부모님댁 안부·원격 모니터링** (센서 → MQTT → 폰 알림/대시보드)
> 운영 아키텍처(메모리 확정): **ESP32 → (Wi-Fi/MQTT) → 맥 Mosquitto broker → Tailscale Funnel(TLS) → 폰 IoT MQTT Panel**

---

## 0. 지금 당장 (하드웨어 0개로 가능) — 도착 전에 끝내둘 것

### (1) 튜토리얼 받기 — 제일 먼저
- Freenove 783p 튜토리얼 PDF 다운로드 (상자 안 QR/링크, 또는 `github.com/Freenove` 의 `Freenove_ESP32_S3_WROOM_Board` 저장소)
- 받으면 `~/esp32-iot/docs/` 에 넣어두기
- **목차에서 우리 목표와 직결되는 장만 표시:**
  - 카메라 웹서버 / TCP 서버 장 (≈32~34장) → 원격 모니터링 핵심
  - Wi-Fi 연결 / HTTP 장 → 통신 검증
  - Servo / DC Motor / Relay / Stepper 장 → 물리 제어

### (2) 개념만 미리 잡기 (읽기 학습)
보드를 안 만져도 이해할 수 있는 것들:
- **GPIO란?** 핀에 HIGH(3.3V)/LOW(0V)를 줘서 부품을 켜고 끔. ESP32-S3는 44핀이라 여유 큼.
- **PWM** — 핀을 빠르게 켰다 껐다 해서 "밝기/속도/각도"를 흉내. LED 밝기, 모터 속도, 서보 각도가 전부 PWM.
- **3.3V 로직** — ESP32는 3.3V. 5V 부품 연결 시 주의(릴레이 모듈은 보통 5V 구동 + 3.3V 신호 OK).
- **액추에이터 4종 차이:**
  | 부품 | 동작 | 쓰임새 |
  |---|---|---|
  | DC모터 | 그냥 회전(방향·속도) | 팬, 바퀴 |
  | 서보 | 0~180° 정확한 각도 | 문/밸브/깃발 |
  | 스텝퍼 | 정밀 단계 회전 | 블라인드, 카메라 회전 |
  | 릴레이 | 220V 가전 ON/OFF 스위치 | 전등, 콘센트 |

### (3) 개발환경 미리 설치 (이미 진행 중/예정)
- `arduino-cli` + ESP32-S3 코어 → 코드 **컴파일 검증**까지 도착 전에 끝 (업로드만 도착 후)
- 스케치 스캐폴드는 `~/esp32-iot/sketches/` 에 준비됨 (아래 5장 참고)

---

## 1. 도착 첫날 — "통신 검증"까지 (30분)

목표: **보드가 살아있고 + Wi-Fi로 통신된다** 를 확인. (사용자 1순위 요구)

```
[ ] 1. USB-C 케이블로 맥에 연결
[ ] 2. 포트 확인:  arduino-cli board list   (또는 ls /dev/cu.usb*)
       → /dev/cu.usbmodem… 또는 /dev/cu.wchusbserial… 보이면 OK
       → 안 보이면: CH343/CP210x USB 드라이버 설치 필요 (아래 트러블슈팅)
[ ] 3. 01-blink 업로드 → 온보드 RGB LED 깜빡이면 보드 정상
[ ] 4. 02-wifi-test 업로드 (Wi-Fi SSID/PW 입력 후)
       → 시리얼 모니터에 IP 주소 찍히면 "통신 검증 완료" ✅
```

업로드 명령 (스캐폴드 README에 그대로 있음):
```bash
arduino-cli compile --fqbn esp32:esp32:esp32s3 sketches/01-blink
arduino-cli upload  --fqbn esp32:esp32:esp32s3 -p /dev/cu.usbXXXX sketches/01-blink
arduino-cli monitor -p /dev/cu.usbXXXX -c baudrate=115200
```

---

## 2. 도착 1~2일차 — 물리 제어 한 번씩

각 부품을 *한 번씩* 동작시켜 감 잡기 (FNK0082는 종류별 1개씩 들어있음):
```
[ ] 서보 1개 sweep (0→180→0)      → 03-servo
[ ] 릴레이 똑딱 (1초 ON/OFF)       → 04-relay  (찰칵 소리로 확인)
[ ] DC모터 정/역회전
[ ] PIR로 사람 감지 → 시리얼 출력  (부모님댁 안부의 핵심 센서)
[ ] DHT 온습도 읽기
```

---

## 3. 도착 3일차~ — 목표 직결 미니 프로젝트

원격 모니터링 1차 완성:
```
[ ] 카메라 웹서버 예제 업로드 → 폰 브라우저로 영상 확인 (튜토리얼 32~34장)
[ ] PIR + DHT 값을 시리얼/웹으로 노출
```

---

## 4. 운영 단계 (별도 세션에서) — ESPHome + MQTT + Tailscale

> ⚠️ ESPHome는 Python 3.14에서 호환 이슈 가능 → 전용 venv(3.12)나 Docker로 분리 설치 예정.

```
[ ] 맥에 Mosquitto broker 설치 (brew install mosquitto)
[ ] ESPHome YAML로 펌웨어 작성 (Arduino C 대신 선언형)
[ ] 센서 → MQTT publish → 맥 broker
[ ] Tailscale Funnel로 broker를 외부 TLS 노출 (0원)
[ ] 폰 "IoT MQTT Panel" 앱에서 대시보드 + 알림
```
이 단계는 부품 감 잡은 뒤 진행. **nacho=Notion / tako=Jira 분리 원칙** 유지(이 프로젝트와 무관하게).

---

## 트러블슈팅 메모

- **포트 안 보임** → USB 드라이버: 보드 칩이 CH343이면 WCH 드라이버, CP2102면 Silicon Labs 드라이버. `ls /dev/cu.*` 로 확인.
- **업로드 실패** → BOOT 버튼 누른 채 RESET → BOOT 떼기(다운로드 모드). 또는 `--fqbn …:esp32s3:CDCOnBoot=cdc` 옵션.
- **시리얼 안 찍힘** → baudrate 115200 확인, `Serial.begin(115200)` 후 보드 RESET 1회.
- **온보드 LED 안 깜빡** → S3는 단순 LED가 아니라 **RGB(WS2812, GPIO48)**. `neopixelWrite()` 사용 (01-blink가 그렇게 짜여 있음).
