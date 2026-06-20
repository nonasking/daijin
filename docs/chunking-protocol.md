# daijin 오디오 청킹 프로토콜 (Phase 2)

> 문제: 음성 클립은 100~180KB인데 ESP32의 MQTT 버퍼(PubSubClient)는 작아서 통째로 못 보냄.
> 해결: 오디오를 작은 조각으로 쪼개 MQTT로 보내고, 받는 쪽에서 합친다.

## 고정 오디오 포맷 (양방향 동일 — 헤더 협상 불필요)
- **PCM 16-bit signed little-endian, 16000 Hz, mono** (raw, WAV 헤더 없음)
- 16k·16bit·mono = **32 KB/초** → 한 조각 4096B ≈ **0.128초**
- 브레인(맥)이 필요할 때만 WAV 헤더를 씌움(whisper 입력)·벗김(전송)

## 토픽
| 토픽 | 방향 | 내용 |
|---|---|---|
| `daijin/audio/in`  | 디바이스 → 브레인 | 녹음 PCM 청크 |
| `daijin/audio/out` | 브레인 → 디바이스 | 답변 PCM 청크 |
| `daijin/text/in`   | 브레인 → (디버그) | 전사된 내 말 |
| `daijin/text/out`  | 브레인 → (디버그) | daijin 답(글) |

## 메시지 포맷 (각 MQTT 메시지 = 청크 1개)
```
바이트 0      clipId   (uint8)  한 음성 클립 식별 (0~255 순환)
바이트 1~2    seq      (uint16, big-endian)  청크 순번 0,1,2...
바이트 3      flags    (uint8)  bit0 = 마지막 청크(1)
바이트 4~     payload  PCM 데이터 (최대 4096B)
```
- 헤더 4B + PCM ≤4096B → MQTT 메시지 ≈ 4100B
- **QoS 1** (유실 방지). 같은 토픽·발행자는 순서 보존 → seq로 재정렬, 중복 seq 무시
- ESP32: `mqtt.setBufferSize(4200)` 이상

## 송신 (보내는 쪽)
1. 오디오를 4096B PCM 단위로 분할
2. clipId 고정(이번 클립), seq 0부터 증가, 각 청크 발행
3. 마지막 청크에 flags bit0=1
4. (선택) 끝에 짧은 지연으로 브로커 폭주 방지

## 수신 (받는 쪽)
1. 같은 clipId 청크를 seq 순으로 버퍼에 모음 (ESP32는 PSRAM 활용)
2. flags.last 받으면 → 클립 완성
3. **디바이스**: PCM을 I2S로 바로 write (raw PCM이라 디코더 불필요, 스트리밍 재생 가능)
   - 청크 도착할 때마다 I2S로 흘려보내면 전체 버퍼링 없이 저지연 재생
4. **브레인**: PCM에 WAV 헤더 붙여 임시파일 → whisper

## ESP32 재생 핵심 (왜 raw PCM인가)
- TTS 출력이 **PCM 16k/16bit/mono** 라, ESP32가 `i2s_write()` 로 **샘플을 I2S 앰프에 직접** 보내면 됨 → ESP32-audioI2S 디코더·SD 불필요, 스트리밍 재생.
- 녹음도 INMP441(I2S in)에서 PCM이 바로 나오므로, 그대로 청크로 발행.

## 흐름 요약
```
[버튼 누름] INMP441 I2S 녹음(PCM)
  → 4KB 청크로 daijin/audio/in 발행 (seq, last)
[브레인] 청크 재조립 → WAV → whisper → Claude → TTS(PCM)
  → 4KB 청크로 daijin/audio/out 발행
[디바이스] 청크 받는 대로 I2S로 재생 (스트리밍)
```

## 구현 메모
- 브레인(daijin_mqtt.py): 현재는 "통째 클립" 처리(파이썬 가짜 디바이스용). Phase 2에서 **청크 재조립/분할**로 업데이트 필요.
- 청크 크기 4096은 시작값 — ESP32 RAM·지연 보고 2048~8192 사이 조정.
- 동시성: 한 번에 한 클립(push-to-talk)이라 clipId만으로 충분. 겹침 없음.
- 끝점: 마지막 청크 유실 대비 brain은 일정 시간 무수신이면 타임아웃으로 클립 종료 처리(선택).
