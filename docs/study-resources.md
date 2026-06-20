# IoT 입문 → 자가 트러블슈팅 학습 자료 (큐레이션)

> 목표: **스스로 문제를 해결할 수 있는 수준.** 그래서 "읽을 것"보다 "고장났을 때 어디를 보는지"에 무게.
> 순서대로 따라가면 됨. 각 항목에 *왜 보는지*와 *예상 시간* 표기.

---

## ⭐ 0순위 — 사용자 키트 공식 문서 (이것부터, 무조건)

**Freenove FNK0082 온라인 튜토리얼 (웹에서 바로 읽기, PDF 다운로드 불필요)**
- https://docs.freenove.com/projects/fnk0082/en/latest/index.html
- **왜 1순위**: 다른 자료는 "일반 ESP32"지만, 이건 *사용자가 산 바로 그 키트*. 부품·배선·코드가 100% 일치. 헷갈릴 일이 없음.
- **보는 법**: 왼쪽 목차에서 `C`(Arduino) 코드 트랙 따라가기. 1장 LED → 통신 → 센서 → 32~34장 카메라/TCP(원격 모니터링 핵심).
- GitHub(코드 원본 zip): https://github.com/Freenove/Freenove_Ultimate_Starter_Kit_for_ESP32_S3
- 예상: 도착 전 **개념 장만** 훑기(2~3시간), 나머지는 부품 손에 쥐고.

---

## 1순위 — ESP32 개념을 "영어지만 그림 많은" 최고 사이트

**Random Nerd Tutorials (RNT)** — ESP32 입문계의 사실상 표준
- 시작하기: https://randomnerdtutorials.com/getting-started-with-esp32/
- 초보 10개 프로젝트: https://randomnerdtutorials.com/esp32-projects-for-beginners/
- 250+ 프로젝트 색인(검색용 사전처럼): https://randomnerdtutorials.com/projects-esp32/
- 웹서버 만들기(원격 모니터링 직결): https://randomnerdtutorials.com/esp32-web-server-arduino-ide/
- **왜**: 설명이 *단계별 + 사진 + 전체 코드*라 영어가 약해도 따라감. 막히면 "내 부품 + RNT 검색"이 거의 항상 답을 줌.

---

## 2순위 — 한국어로 개념 다지기 (영어가 부담될 때 병행)

- **MakeitNow** ESP32 아두이노 설치·세팅: https://www.makeitnow.kr/129
  (버튼으로 LED, DHT11 온습도 등 우리 스케치와 겹치는 입문 강좌)
- **모두의 메이커** ESP32 아두이노 IDE 사용법: https://makerspace.steamedu123.com/entry/ESP32-ESP32-아두이노-IDE-에서-사용하기
- **WikiDocs** ESP32 하드웨어/기본 설정: https://wikidocs.net/323448
- **Happy Creative** ESP32 매뉴얼(핀·센서·블루투스 정리): https://happycreative.co.kr/manual/detail.php?idx=51
- **왜**: 용어를 한국어로 한 번 잡아두면 영어 자료 이해 속도가 빨라짐.

---

## 3순위 — MQTT (운영 단계, 부품 감 잡은 뒤)

ESP32가 *서로/폰과* 대화하는 약속. 우리 최종 아키텍처(맥 broker)의 언어.
- **HiveMQ MQTT Essentials** 1편(개념 가장 쉬움): https://www.hivemq.com/blog/mqtt-essentials-part-1-introducing-mqtt/
- 전체 개념 허브: https://www.hivemq.com/mqtt/
- 공식 입문: https://mqtt.org/getting-started/
- **핵심 단어 3개만 먼저**: Publish(보냄)·Subscribe(구독)·Topic(주제). "센서가 `home/temp`라는 주제로 25를 publish → 폰이 그 주제를 subscribe → 받음." 이게 전부.
- **왜 나중에**: 부품 1개도 안 움직여봤는데 MQTT부터 하면 추상적이라 튕김. LED·센서 먼저.

---

## 🔧 4순위 — 트러블슈팅 (목표의 핵심! 북마크 필수)

고장은 *반드시* 나요. 그때 당황 안 하려고 미리 봐두는 게 아니라, **막혔을 때 펴보는 사전**으로 저장.

- **RNT ESP32 트러블슈팅 종합 가이드** ⭐: https://randomnerdtutorials.com/esp32-troubleshooting-guide/
- **업로드 실패 "Failed to connect / Fatal error" 해결**: https://lastminuteengineers.com/esp32-fatal-error-fix-tutorial/

### 가장 흔한 3대 문제 — 미리 외워둘 가치 있음
| 증상 | 1순위 의심 | 해결 |
|---|---|---|
| **포트가 안 잡힌다** | ① 충전전용 USB 케이블 ② USB 드라이버 없음 | 데이터 케이블로 교체 / CH343·CP210x 드라이버 설치 |
| **업로드 중 "Failed to connect"** | 보드가 다운로드 모드 진입 실패 | **BOOT 버튼 누른 채** 업로드 시작 → "Connecting…" 뜨면 떼기 |
| **시리얼에 글자 깨짐/안 나옴** | baudrate 불일치 | 모니터를 `115200`으로, 코드의 `Serial.begin(115200)`과 맞추기 |

> 이 표 3개만 알아도 입문자 문제의 80%는 스스로 해결돼요.

---

## 🧠 자가 트러블슈팅 "사고법" — 자료보다 중요한 습관

문제를 만나면 이 순서로 *좁혀나가는* 게 트러블슈팅의 본질이에요:

1. **분리해서 확인** — "보드 문제? 코드 문제? 배선 문제?"를 따로 떼서 테스트.
   - 예: 센서가 이상하면 → 01-blink(코드·보드 정상 확인) → 배선만 의심.
2. **마지막에 바꾼 것 의심** — 방금 전까지 됐는데 안 되면, *방금 바꾼 한 가지*가 범인.
3. **시리얼에 찍어보기** — `Serial.println()`으로 "여기까지 왔나?" "이 값이 뭐지?"를 눈으로 확인. 보드의 유일한 입.
4. **에러 메시지 그대로 검색** — 메시지 한 줄을 따옴표로 묶어 구글에. 99%는 누군가 이미 겪음(RNT/Arduino 포럼/espressif GitHub Issues).
5. **그래도 막히면 나한테** — 에러 전문 + 배선 사진 + 방금 한 것 알려주면 같이 좁혀나감.

---

## 📅 추천 학습 순서 (배송 기다리는 며칠)

```
Day 1-2 : RNT "시작하기"(1순위) + 한국어 1편(2순위)로 ESP32가 뭔지 + 용어
Day 2-3 : Freenove FNK0082 온라인 문서(0순위) 목차 훑고 LED·통신 장 읽기
Day 3+  : 트러블슈팅 가이드(4순위) 한 번 읽어 "이런 문제가 있구나" 지도 그리기
        : (여유되면) MQTT 1편으로 개념만 맛보기
보드 도착 : day1-checklist.md → 직접 한 줄씩 입력하며 실습
```
> 다 외울 필요 없어요. **"이 문제는 저기서 봤지" 하고 다시 찾아갈 지도**만 머리에 그려지면 목표 달성.
