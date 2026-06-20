# ESP32-S3 스캐폴드 사용법

보드: **Freenove FNK0082 (ESP32-S3-WROOM)** · FQBN: `esp32:esp32:esp32s3`
모든 스케치는 도착 전 **컴파일 검증 완료** (✅ = 빌드 통과 확인됨). 보드 꽂으면 **업로드만** 하면 됨.

## 공통 3-스텝
```bash
# 1) 포트 찾기 (보드 USB 연결 후)
arduino-cli board list          # /dev/cu.usbmodem… 또는 /dev/cu.wchusbserial… 확인
PORT=/dev/cu.usbXXXX            # ← 위에서 본 포트로 교체

# 2) 컴파일 + 업로드 (예: 01-blink)
arduino-cli compile --fqbn esp32:esp32:esp32s3 01-blink
arduino-cli upload  --fqbn esp32:esp32:esp32s3 -p $PORT 01-blink

# 3) 시리얼 모니터 (출력 확인)
arduino-cli monitor -p $PORT -c baudrate=115200
```
> 업로드 실패 시: BOOT 버튼 누른 채 RESET 눌렀다 떼고 BOOT 떼기(다운로드 모드) → 재시도.

## 스케치 목록 & 배선

| # | 스케치 | 목적 | 배선 (신호핀) | 라이브러리 |
|---|---|---|---|---|
| 01 | `01-blink` | 보드 살아있는지 | 온보드 RGB(GPIO48) | 없음 ✅ |
| 02 | `02-wifi-test` | **통신 검증**(1순위) | 없음(Wi-Fi) | 없음 ✅ |
| 03 | `03-servo` | 서보 각도 제어 | GPIO13 = 주황선 / 5V·GND | ESP32Servo ✅ |
| 04 | `04-relay` | 220V 가전 스위치 | GPIO14 = IN / 5V·GND | 없음 ✅ |
| 05 | `05-pir` | 인체감지(안부) | GPIO4 = OUT / 5V·GND | 없음 ✅ |
| 06 | `06-dht` | 온습도(환경) | GPIO5 = DATA / 3.3V·GND | DHT sensor library ✅ |

> 핀 번호는 예시 — 원하는 GPIO로 바꿔도 됨(코드 상단 상수 수정). 배선 후 실제 핀과 코드 일치만 확인.

## 추천 순서 (도착 후)
1. **01 → 02**: 보드 정상 + Wi-Fi 통신 확인 (여기까지 도착 첫날, `docs/day1-checklist.md`)
2. **03 → 04**: 서보·릴레이로 물리 제어 첫 감
3. **05 → 06**: 모니터링 센서 (부모님댁 안부·환경)
4. 이후 카메라 웹서버(튜토리얼 32~34장) → ESPHome+MQTT 운영 단계

## ⚠️ 02-wifi-test 쓰기 전
`02-wifi-test.ino` 상단 `SSID` / `PASSWORD`를 본인 와이파이로 수정.

## ⚠️ 04-relay 안전
처음엔 **220V 부하 없이** 똑딱임(찰칵 소리)만 확인. 220V 연결은 충분히 학습한 뒤에만.
