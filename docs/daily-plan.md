# ESP32-S3 일별 학습 계획 (키트 도착 후)

> 교재: Freenove FNK0082 `C/C_Tutorial.pdf` (437p) 기준. 챕터 번호 = 교재/예제 폴더와 동일.
> 도구: **arduino-cli** (터미널). 교재의 Arduino IDE 메뉴 설정은 CLI 옵션으로 번역해 진행.
> 목표: 부모님댁 안부·원격 모니터링 + 스스로 트러블슈팅.
> 페이스: 하루 1~2시간 가정. 빠르면 합치고, 막히면 늘려도 됨. **순서가 중요, 속도는 자유.**

⚠️ **S3 시리얼 주의**: ESP32-S3는 USB로 시리얼 출력을 보려면 `CDCOnBoot=cdc` 옵션이 필요.
업로드 FQBN을 **`esp32:esp32:esp32s3:CDCOnBoot=cdc`** 로 쓰면 `arduino-cli monitor`에 메시지가 보임.
(이게 S3 입문자 최대 함정 — "코드는 도는데 시리얼이 빈 화면")

---

## Day 0 — 오늘: "보드 살아있음" 확인 ⭐ (30~40분)
교재: Preface(CH343/환경설정/GPIO) + Chapter 0 Blink (p31)

```
[ ] 1. 물리 준비 — 브레드보드에 끼워져 온 칩들 살살 빼기
       (Start Here.pdf 지시: 작은 드라이버로 양 끝 번갈아 살짝, 한 번에 들지 말 것)
[ ] 2. 데이터 되는 USB-C 케이블로 보드 ↔ 맥 연결 → 보드 LED 점등 확인
[ ] 3. (Claude가) 포트 인식 확인  →  안 잡히면 케이블/드라이버 분기
[ ] 4. (Claude가) 첫 Blink 업로드 → 온보드 RGB가 빨강·초록·파랑 깜빡 = 보드 정상 ✅
[ ] 5. 시리얼 모니터로 "boot OK" 메시지 확인 → USB 통신까지 검증
```
👉 여기까지 = 오늘의 성공. 코드 한 줄 안 짜도 됨(준비된 01-blink 사용).

## Day 1 — 입출력 기본 + 디버깅 (1~1.5시간)
교재: Ch1 LED(p40) · Ch2 Button&LED(p47) · Ch8 Serial(p96)
```
[ ] Ch1: 브레드보드에 외부 LED 1개 — 첫 배선 경험 (Pinout 그림 보며)
[ ] Ch2: 버튼으로 LED 켜기 — digitalRead(입력) 개념
[ ] Ch8: Serial.print로 값 출력 — "보드의 입"으로 디버깅하는 법
```
🎯 핵심 습관: **digitalWrite(출력) ↔ digitalRead(입력)** 의 차이 체득. IoT의 전부.

## Day 2 — 아날로그 & PWM (1~1.5시간)
교재: Ch4 Analog&PWM(p60) · Ch5~6 RGB LED(p71) · Ch9 ADC(p102)
```
[ ] Ch4: 호흡 LED — PWM(밝기 조절)의 원리 (LED밝기=모터속도=서보각도, 같은 원리)
[ ] Ch5~6: RGB 색 섞기
[ ] Ch9: 가변저항 값 읽기 — analogRead(아날로그 입력)
```

## Day 3 — 모니터링 센서 ⭐⭐ (목표 직결, 1.5~2시간)
교재: Ch12 조도(p130) · Ch13 온도(p135) · Ch24 온습도 · Ch25 PIR 인체감지
```
[ ] Ch25: PIR 인체감지 — "부모님댁 움직임 감지"의 핵심 부품
[ ] Ch24: 온습도(DHT) — 환경 모니터링
[ ] Ch12: 조도 — 불 켜짐/낮밤 감지
```
🎯 이 날부터 "내 목표 부품"을 직접 만짐. (라이브러리 설치는 Claude가 그때 처리)

## Day 4 — 네트워크 통신 ⭐ (사용자 1순위, 1.5시간)
교재: Ch30 WiFi(Station/AP) · Ch31 WiFiClient/Server
```
[ ] Ch30.1: Wi-Fi 접속 → 시리얼에 IP 출력 (= 준비된 02-wifi-test)
[ ] Ch31: 보드를 웹서버로 → 폰 브라우저로 보드 상태 보기
```

## Day 5 — 카메라 원격 보기 ⭐⭐ (원격 감시, 1.5시간)
교재: Ch32 CameraWebServer · Ch33 CameraTcpServer
```
[ ] Ch32.1: 카메라 웹서버 업로드 → 폰으로 실시간 영상 (같은 Wi-Fi)
```
🎯 "부모님댁 영상 확인"의 1차 형태 완성.

## Day 6 — 물리 제어 (필요 시, 1~1.5시간)
교재: Ch17 릴레이/모터 · Ch18 서보 · Ch19 스텝퍼
```
[ ] Ch17.1: 릴레이 똑딱 (220V 부하 없이 소리만 — 안전)
[ ] Ch18.1: 서보 각도 제어
```

## Day 7+ — 통합 & 운영 (별도 진행)
```
[ ] 미니 통합: PIR + 온습도 + Wi-Fi → 폰으로 "움직임/온도" 한눈에
[ ] 운영 단계: ESPHome + 맥 MQTT broker + Tailscale Funnel + 폰 IoT MQTT Panel
       (ESPHome는 Python 3.14 호환 위해 별도 venv/Docker — 그때 Claude가 세팅)
```

---

## 막힐 때 (스스로 트러블슈팅 3종)
| 증상 | 해결 |
|---|---|
| 포트 안 잡힘 | 데이터 케이블 확인 → CH343 드라이버(`CH343/MAC/`, 최신은 wch-ic.com) |
| 업로드 실패 | BOOT 누른 채 업로드 → "Connecting…" 뜨면 떼기 |
| 시리얼 빈 화면 | FQBN에 `:CDCOnBoot=cdc` 붙였는지, 모니터 baud 115200 |
| 부품 안 됨 | 배선·칩 방향 확인 (`ESP32S3_Pinout.png`), 라이브러리 설치 여부 |

참고문서: `study-resources.md`(외부 자료), `sketches/README.md`(준비된 코드), 교재 `C/C_Tutorial.pdf`.
