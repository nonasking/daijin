# 📦 도착 첫날 체크리스트 (30분, 통신 검증까지)

> 목표: **보드 살아있음 + Wi-Fi 통신됨** 확인. 코드·환경은 이미 준비 끝 → 업로드만.

```
[ ] 1. USB-C 케이블로 보드 ↔ 맥 연결
       (데이터 가능 케이블! 충전전용 케이블이면 포트 안 잡힘)

[ ] 2. 포트 확인
       arduino-cli board list
       → /dev/cu.usbmodem… 또는 /dev/cu.wchusbserial… 보이면 OK
       → 안 보이면 ↓ 드라이버

[ ] 3. (포트 안 보일 때만) USB 드라이버 설치
       보드 칩이 CH343 → WCH 드라이버 / CP2102 → Silicon Labs 드라이버
       설치 후 케이블 재연결

[ ] 4. 보드 정상 확인 — 01-blink
       cd ~/esp32-iot/sketches
       PORT=/dev/cu.usbXXXX        # 2번에서 본 포트
       arduino-cli upload --fqbn esp32:esp32:esp32s3 -p $PORT 01-blink
       → 온보드 RGB가 빨강·초록·파랑 깜빡이면 보드 정상 ✅

[ ] 5. 통신 검증 — 02-wifi-test (1순위 목표)
       먼저 02-wifi-test/02-wifi-test.ino 에 Wi-Fi SSID/PW 입력
       arduino-cli upload --fqbn esp32:esp32:esp32s3 -p $PORT 02-wifi-test
       arduino-cli monitor -p $PORT -c baudrate=115200
       → 시리얼에 IP 주소 찍히면 "통신 검증 완료" ✅✅

[ ] 6. 폰/PC 브라우저에서 그 IP로 접속
       → "ESP32-S3 살아있음" 페이지 뜨면 끝. 같은 Wi-Fi망이면 폰에서도 보임.
```

여기까지 되면 다음은 `sketches/README.md`의 "추천 순서" 3·5·6번(서보·PIR·온습도)으로.
업로드 실패 시: BOOT 누른 채 RESET → BOOT 떼기(다운로드 모드) 후 재시도.
