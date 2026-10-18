# 설계: 척수 층(장부·추동·기억)과 로컬 음성 스택

(2026-10-10. 지금 서버와 클라우드 브레인(Claude)을 그대로 둔 채 자아의 자리를 모델 밖에 만들고, 음성 인식·합성을 로컬로 바꾼다. 이 문서가 구현의 기준이다. 나중에 모델을 로컬로 바꿔도 이 층은 그대로 남는 것이 목표다.)

## 0. 원칙

1. **모델은 말만 한다.** 몸의 변수, 먼저 말 걸지 말지, 거절 뒤에 뭘 바꿀지, 무엇을 기억할지는 코드와 데이터가 정한다. 나중에 Claude 를 로컬 모델로 바꿔도 이 층은 그대로다.
2. **장부는 버스를 본다.** 누가 먼저 말하든(플릿 워처, 새 추동 엔진, 사람이 say.sh 로) `daijin/say` 나 `daijin/ask` 로 나간다. 장부는 그 토픽과 `daijin/text/in` 을 구독해서 적는다. 말하는 쪽은 장부를 몰라도 된다.
3. **틀리게 묻는 것은 기본값이다.** 설계할 것은 묻는 정확도가 아니라 거절 뒤의 동작이다. 엄마와 아기도 상호작용의 70% 이상을 어긋난 채로 보내고 중요한 것은 복구다(Tronick & Cohn 1989), 먼저 말 거는 에이전트에 대한 가장 흔한 반응은 거절이 아니라 무시다(Abbas 외, CHI '26). 그래서 거절을 먼저 적고, 정책을 거기서 바꾼다.
4. **브레인 변경은 최소로.** 새 기능은 새 프로세스(initiative, reflect)에 두고, 브레인에는 기록 한 줄과 상태 주입 한 블록만 넣는다. 지금 브레인이 잘 도는 것을 깨지 않는다.
5. **사람 승인이 있는 곳은 한 군데.** 인격(persona) 변경. 나머지는 자동이되 전부 기록된다.

## 1. 전체 그림

```
디바이스 ──audio/in──▶ daijin-brain ──audio/out──▶ 디바이스
                        │  (STT → Claude → TTS)
                        │  턴마다 episodes 기록, 턴 시작에 "지금 상태" 블록 주입
                        ▼
                   voice/daijin.db (SQLite)
                        ▲
   daijin/status ───────┤
   daijin/dev/+/status ─┤   daijin-initiative (신규, 상시)
   daijin/say, ask ─────┤     추동 계산 → 허용 필터 → daijin/say|ask 발행
   daijin/text/in ──────┘     먼저 말한 것(asks) 열고, 응답 오면 닫고 분류
                        ▲
                   daijin-reflect (신규, 밤 1회, systemd timer)
                     하루치 episodes·asks → 기억 추출, 장부 재분류, 인격 diff 제안 (승인 대기)

   daijin-fleetwatch (있음): 그대로. 그 보고도 say 로 나가므로 장부에 자동으로 잡힌다.
```

프로세스는 둘이 는다. 둘 다 `daijin_common` 의 브로커 클라이언트를 쓰고 systemd 사용자 서비스/타이머로 돈다. 브레인은 DB 를 쓰지만 DB 가 없어도 돈다(열기 실패면 기록만 건너뜀).

## 2. 데이터: `voice/daijin.db`

SQLite 한 파일, WAL 모드, gitignore. 서버에 sqlite 3.45 가 있고 파이썬 표준 라이브러리로 충분하다. 외부 패키지 없음.

```sql
-- 한 턴 = 한 줄. 브레인이 쓴다. 지금 journald 에만 있는 것을 구조화한다.
CREATE TABLE episodes (
  id        INTEGER PRIMARY KEY,
  ts        INTEGER NOT NULL,          -- epoch
  kind      TEXT NOT NULL,             -- voice | ask | say
  user_text TEXT,                      -- 들린 말 (voice) / 넘긴 상황 (ask)
  reply     TEXT,                      -- 다이진이 한 말 (문장들 합침)
  stt_conf  REAL, rms INTEGER, clip_s REAL,
  turn_json TEXT,                      -- Turn.done 의 JSON 그대로 (단계별 시간)
  ask_id    INTEGER REFERENCES asks(id) -- 이 턴이 어떤 먼저 말 걸기에 대한 응답이면
);

-- 먼저 말 건 것 = 장부의 한 줄. initiative 가 버스를 보고 쓴다.
CREATE TABLE asks (
  id             INTEGER PRIMARY KEY,
  ts             INTEGER NOT NULL,
  drive          TEXT NOT NULL,        -- contact | node_loss | body | fleet(워처) | manual(say.sh) | unknown
  drive_value    REAL,                 -- 그때의 추동 세기 (0~1)
  trigger        TEXT,                 -- 사람이 읽을 이유: "레드 7시간째 오프라인"
  channel        TEXT NOT NULL,        -- say | ask
  text           TEXT NOT NULL,        -- 실제로 나간 문장 (ask 면 에이전트가 만든 답, text/out 으로 잡는다)
  presence       TEXT,                 -- unknown | present | absent  (지금은 전부 unknown. 재실 센서가 생기면 채운다)
  response_class TEXT,                 -- engaged | acknowledged | refused_context | negative | unanswered  (창이 닫힐 때 확정)
  response_ts    INTEGER, latency_s REAL, response_text TEXT,
  repair_strategy TEXT, repair_outcome TEXT,   -- 거절 뒤 다음 번에 한 복구: explain | promise, 그 결과
  wanted_to_ask_less INTEGER,          -- 프랑크퍼트의 2차 의욕 칸: 덜 묻고 싶었는데 물었나. NULL = 모름. reflect 가 채운다
  label_source   TEXT                  -- rule | reflect  (분류를 규칙이 했는지 밤 반성이 고쳤는지)
);

-- 항상성 변수 시계열. initiative 가 60초마다 쓴다. 값은 0~1 로 정규화한 "허기".
CREATE TABLE drives (
  ts INTEGER NOT NULL, name TEXT NOT NULL, value REAL NOT NULL, inputs TEXT  -- inputs: 원 수치 JSON
);
CREATE INDEX drives_ts ON drives(ts);

-- 추동별 정책 상태. 장부가 바꾼다.
CREATE TABLE policy (
  drive          TEXT PRIMARY KEY,
  base_interval_s INTEGER NOT NULL,   -- 이 추동으로 다시 묻기까지 기본 간격
  backoff_level  INTEGER NOT NULL DEFAULT 0,   -- 무시당할 때마다 +1, 응답 오면 0
  last_ask_ts    INTEGER,
  muted_until    INTEGER,             -- negative 뒤 24시간 등
  avoid_hours    TEXT                 -- 거절이 몰린 시간대 JSON, 예 ["09","10"]
);

-- 가중치 밖의 기억 (MemGPT·Zep 계열의 텍스트 기억). reflect 가 쓰고, 브레인이 상태 블록에 몇 개 넣는다.
CREATE TABLE memories (
  id INTEGER PRIMARY KEY, ts INTEGER NOT NULL,
  kind TEXT NOT NULL,                 -- fact | preference | rule | event
  text TEXT NOT NULL,                 -- 한 문장
  source_episode INTEGER, importance INTEGER,   -- 1~10, reflect 가 매김 (Generative Agents 식)
  valid_from INTEGER, valid_to INTEGER,         -- 대체되면 valid_to 를 채운다, 지우지 않는다 (Zep 식)
  confidence REAL
);

-- 인격 파일의 버전. 제안은 사람이 승인해야 current 가 된다.
CREATE TABLE persona_versions (
  id INTEGER PRIMARY KEY, ts INTEGER NOT NULL,
  text TEXT NOT NULL, diff_summary TEXT,
  proposed_by TEXT NOT NULL,          -- reflect | human
  status TEXT NOT NULL                -- proposed | current | rejected | superseded
);
```

장부에 "무시"가 두 칸이 아니라 한 칸(`unanswered`)인 이유: 지금은 사람이 집에 있는지 모른다. 무응답을 `ignored`(피로)로 적는 것은 재실 센서가 생긴 뒤에만 정직하다. 그 전까지는 `unanswered` + `presence=unknown` 으로 적고, 정책은 그것을 **약한** 거절로 취급한다(아래 §5).

## 3. 추동 v1: 지금 재지는 것만

다마지오의 전제대로 몸에서 바로 나오는 것만 넣는다. 셋.

| 이름 | 입력 (토픽) | 값 (0~1) | 말 걸기 후보 |
|---|---|---|---|
| `contact` 외로움 | 마지막 voice episode 로부터 경과 | `min(1, 경과/24h)` 를 정책 간격으로 눌러 씀 | "오늘은 아직 얘기를 못 했네." (say, 고정 문장) |
| `node_loss` 상실 | `daijin/dev/+/status` 의 online=false 지속 | 가장 오래 꺼진 노드의 `min(1, 경과/24h)` | "{노드}가 {n}시간째 꺼져 있어. 그냥 둔 거야?" (ask, 에이전트가 표현) |
| `body` 몸 | `daijin/status` 의 temp_c, rssi, 그리고 status 가 끊긴 시간 | temp: `(temp-50)/20` 클램프, rssi: `(-60-rssi)/20` 클램프, 둘 중 큰 것 | "나 좀 뜨거워." / "여기 신호가 약해." (say) |

플릿 워처의 즉시 보고(꺼졌다, 켜졌다)는 그대로 둔다. `node_loss` 는 그 뒤 **오래** 꺼져 있을 때의 추동이고 하루 한 번이다. 즉시 보고와 추동은 다른 시간 축이다.

배터리는 센서가 없어 v1 에서 뺀다. 퓨얼게이지를 달면 `body` 에 입력 하나가 늘어날 뿐이다.

## 4. 허용 필터 (initiative 의 핵심)

60초마다 추동을 계산하고, 값이 문턱을 넘은 추동마다 아래를 **순서대로** 통과해야 말한다. 하나라도 걸리면 그 사실을 로그에 남긴다(왜 안 말했는지도 데이터다).

1. `local_hooks.mute_reports()` 가 True 면 전부 쉼 (지금 워처와 같은 규칙).
2. 조용한 시간: secrets `INITIATIVE_QUIET=23-08` (시작-끝 시). 이 안에선 `body` 의 위험 수준(온도 70도 이상)만 예외.
3. 하루 예산: secrets `INITIATIVE_DAILY_BUDGET=3`. `asks` 에서 오늘 initiative 가 연 줄 수로 센다. 워처의 즉시 보고는 예산에 안 들어간다(장애 보고는 묻는 게 아니라 알리는 것).
4. 추동별 간격: `policy.last_ask_ts + base_interval_s × 2^backoff_level` 이 지나야 한다. 기본 간격은 contact 8시간, node_loss 24시간, body 2시간.
5. 피하는 시간대: `policy.avoid_hours` 에 지금 시가 있으면 쉼.
6. `muted_until` 이 남아 있으면 쉼.
7. 브레인이 지금 말하는 중(`daijin/brain/turn` 이 최근 30초 안에 없고 큐가 비었음을 보장할 수 없으므로)이면 다음 틱으로 미룸. 사람이 말하는 도중에 끼어들지 않는다.
8. 기기가 꺼져 있으면 쉼: `daijin/status` 가 오프라인(LWT)이거나 3분 넘게 소식이 없으면(기기는 60초마다 보낸다). 아무도 못 듣는 말이 장부에 '답 없음'으로 남아 간격만 늘리지 않게 한다. 기기가 다시 켜지면 그때 말한다.

통과하면 채널에 따라 `daijin/say`(고정 문장) 또는 `daijin/ask`(상황 설명을 에이전트에 넘김)로 발행하고 `asks` 에 한 줄을 연다. 이 줄은 **initiative 자신이 버스에서 자기 메시지를 다시 보고** 연다. 그래야 사람이 say.sh 로 말하게 한 것, 워처가 말한 것과 같은 경로로 적힌다. 추동 이름과 이유는 발행 직전에 메모리에 들고 있다가 버스에서 본 메시지에 붙인다(같은 텍스트, 5초 안).

`--dry-run` 으로 띄우면 발행만 하지 않고 "지금 말했을 것"을 로그와 `daijin/initiative/plan`(retained 아님)에 찍는다. **첫 1주일은 dry-run 으로 돌려 보고 켠다.**

## 5. 응답 분류와 정책 변화

`asks` 한 줄이 열리면 응답 창은 **10분** (secrets `INITIATIVE_RESPONSE_WINDOW_S`). 그 안에 들어오는 첫 voice episode 가 응답이다.

| 분류 | 규칙 (STT 텍스트) | 정책 변화 |
|---|---|---|
| `engaged` | 10자 이상이고 아래 둘이 아님 | 그 추동 `backoff_level=0` |
| `acknowledged` | 짧은 긍정: 응, 그래, 알았어, 오케이, 고마워 | 변화 없음 |
| `refused_context` | 바빠, 나중에, 지금은, 이따, 회의, 자야, 조용히 | `backoff_level+1`, 지금 시를 `avoid_hours` 에 추가 (주 2회 이상 겹치면) |
| `negative` | 그만, 시끄러, 조용, 닥쳐, 살살, 짜증, 왜 자꾸 | 그 추동 `muted_until=+24h`, 다음 말 걸기 전 복구 의무 (§6) |
| `unanswered` | 10분 안에 voice 없음 | `backoff_level+1` (presence=unknown 이므로 약하게: 최대 레벨 2 까지만) |

규칙은 `voice/ledger_rules.py` 한 파일의 키워드 목록이고, 밤 반성이 같은 줄을 다시 읽어 분류를 고칠 수 있다(`label_source=reflect`). 규칙과 반성이 다르면 반성을 믿되 그 사실을 남긴다. 몇 주 뒤 둘의 불일치율이 규칙을 고칠 근거다.

`engaged` 가 오면 그 episode 의 `ask_id` 를 채운다. 그러면 "먼저 말 걸어서 시작된 대화"가 나중에 집계된다.

## 6. 복구

`negative` 나 `refused_context` 가 적힌 추동으로 **다음에** 말을 걸 때, 본문 앞에 복구 한 문장을 붙인다. 전략은 둘뿐이고(Esterwood & Robert 2021) 부인은 코드에 없다.

- `explain`: "아까는 {이유}라서 말 걸었어." (추동의 `trigger` 를 그대로 쓴다)
- `promise`: "앞으로는 {바뀐 것}." (정책 변화를 말로: "저녁엔 안 물을게", "하루 한 번만 물을게")

같은 추동에서 복구를 두 번 연속 한 뒤에도 `negative` 면 그 추동은 사람이 풀기 전까지 꺼진다(`muted_until` 을 먼 미래로). 반복된 사과는 힘을 잃는다는 2022 후속 연구 그대로.

## 7. 브레인에 들어가는 것 (작게)

1. **기록 한 줄.** `Turn.done` 자리에서 `selfdb.episode(...)` 호출. 들린 말, 답, 신뢰도, turn JSON. DB 실패는 로그만.
2. **"지금 상태" 블록.** 시스템 프롬프트 끝에 턴마다 다시 만든 200~400자:
   - 몸: 온도, 신호, 켜진 시간, 노드 온/오프 (retained 상태에서 읽음. 브레인이 `daijin/status` 와 플릿 상태를 구독해 메모리에 들고 있으면 도구 호출 없이 바로 안다 → "지어내기" 와 "매번 fleet.sh" 둘 다 줄어든다)
   - 최근 장부 3줄: "어제 저녁 먼저 말 걸었는데 '바빠' 들음" 식. 다이진이 자기 이력을 안다.
   - 기억 상위 5개: `memories` 에서 importance 순, 200자 상한.
   - 인격: `persona_versions.status='current'` 의 텍스트. 지금 `props.local.txt` 가 하던 역할을 이어받는다(처음 버전은 그 파일을 그대로 넣는다).
   이 블록은 Letta 의 sleep-time compute 와 같은 효과를 낸다. 모델이 물어볼 필요가 없는 것을 미리 준다.
3. **상태 구독.** 브레인이 `daijin/status`, `daijin/dev/+/status` 를 구독해 마지막 값을 들고 있는다(fleet_watch 가 이미 하는 일의 축소판). 30줄.

도구 전면 개방은 이 단계에서 건드리지 않는다. 층 2(로컬 LLM)로 갈 때의 일이다.

## 8. 밤 반성 (daijin-reflect)

systemd 타이머, 매일 03:30. `claude -p` 를 **고정 세션과 별개로**(`--resume` 없이) 한 번 돌린다. 대화 세션을 오염시키지 않고, 반성은 반성대로 깨끗한 맥락에서.

입력: 오늘의 `episodes`, `asks`, `drives` 요약, 현재 persona. 출력은 JSON 하나로 강제(`--output-format json` + 프롬프트에 스키마):

```json
{"memories":[{"kind":"preference","text":"사용자는 낮에 잔소리 듣는 걸 싫어한다","importance":7,"source_episode":123}],
 "ask_labels":[{"ask_id":5,"response_class":"negative","wanted_to_ask_less":true}],
 "persona_diff":"...unified diff 또는 빈 문자열...",
 "note":"오늘 한 줄"}
```

- `memories` 는 바로 들어간다. 같은 뜻의 기억이 있으면 옛것의 `valid_to` 를 채운다(지우지 않음).
- `ask_labels` 는 장부를 고치고 `wanted_to_ask_less` 를 채운다. 이 칸은 "덜 묻고 싶었는데 물었는가"를 다이진이 하루를 돌아보며 답하는 자리다. 비어 있으면 비서고, 채워지기 시작하면 그때 다시 얘기하기로 한 그 칸.
- `persona_diff` 는 `persona_versions` 에 `proposed` 로 들어가고 `daijin/brain/alert` 로 "인격 변경 제안 1건" 을 띄운다. `python3 voice/persona.py show|approve|reject` 로 사람이 처리한다. 승인 전엔 아무것도 안 바뀐다.
- 반성이 실패하면(JSON 깨짐, 인증) 알림만 남기고 다음 날로.

비용: Sonnet 5.5 low 로 하루 한 번, 입력 수천 토큰. 무시할 만하다. 나중에 로컬 모델로 바꾸면 밤이라 느려도 된다.

## 9. 로컬 음성 스택

실측 (2026-10-10):

| | 맥 M1 Pro | Hetzner CX23 (2 vCPU) |
|---|---|---|
| Supertonic 3, 한국어, 4 steps | RTF 0.13 | **RTF 0.18~0.23** |
| Supertonic 3, 8 steps | RTF 0.20 | RTF 0.40 |
| whisper.cpp 서버, turbo, 웜 | RTF 0.20~0.22 (json), 0.40 (verbose_json) | 안 잼. CPU 라 수 초~수십 초 예상, 쓰지 않는다 |

### 9.1 TTS: `supertonic` 엔진 — 지금 서버에서 바로

- 브레인 venv 에 `pip install supertonic`. 모델 400MB 는 첫 실행 때 내려받아 `~/.cache/huggingface` 에 둔다(서버에 이미 받아 둠, 184KB 캐시 메타 + 모델).
- `_TTS_ENGINES["supertonic"]`: 프로세스 안 싱글턴(첫 호출에 로드 3.6초, 이후 상주). `synthesize(text, style, total_steps=SUPERTONIC_STEPS(기본 4), lang="ko")` → float32 44.1kHz → ffmpeg 파이프(`-f f32le -ar 44100` → `s16le 16000`)로 PCM 16k. 지금 `mp3_to_pcm16k` 와 같은 모양의 함수 하나.
- 보이스: secrets `SUPERTONIC_VOICE` (프리셋 이름, 기본 M1). 다이진의 ElevenLabs 보이스와 다른 목소리가 된다. **이건 사용자가 들어 보고 정할 일**이다. 프리셋 몇 개를 wav 로 만들어 보내 고르게 한다.
- 순서: `TTS_ENGINE=supertonic` 이면 supertonic → eleven → edge. 지금 엔진 폴백 구조가 그대로 받는다.
- 효과: ElevenLabs 크레딧과 Edge 의 비공식 엔드포인트 의존이 빠진다. 문장당 합성 0.3~0.7초로 지금(eleven 스트리밍 0.3~1초)과 비슷.

### 9.2 STT: `whisper_server` 엔진 — 홈서버(또는 맥)에서

- whisper.cpp 의 `whisper-server -m ggml-large-v3-turbo-q5_0.bin -l ko --port 18080 --host 127.0.0.1` 을 systemd 서비스로 상주. 모델 로드 5초, 이후 웜.
- `/inference` 는 Groq 와 같은 multipart 를 받고 `response_format=verbose_json` 이면 세그먼트별 `avg_logprob` 을 준다(오늘 확인). **`stt_groq` 를 URL 과 인증 헤더만 바꾸는 `stt_http(url, auth)` 로 일반화**하면 두 엔진이 한 함수다. `STT_ENGINE=whisper_server`, `WHISPER_URL=http://127.0.0.1:18080/inference`.
- 순서: whisper_server → groq → whisper-cli. 서버가 안 떠 있으면 Groq 로 자연히 넘어간다.
- 지금 Hetzner 서버에서는 켜지 않는다. 브레인이 집 기계로 옮겨 가는 날 켠다. 그때까지 STT 는 Groq 가 남는 유일한 외부 음성 의존이다.

## 10. 설정 키 (secrets.local.txt 에 추가)

```
# 척수 층
SELF_DB=                      # 비우면 voice/daijin.db
INITIATIVE_QUIET=23-08        # 먼저 말 걸지 않는 시간 (시작시-끝시)
INITIATIVE_DAILY_BUDGET=3     # 하루에 먼저 말 거는 상한 (워처의 장애 보고 제외)
INITIATIVE_RESPONSE_WINDOW_S=600
INITIATIVE_DRY_RUN=1          # 1 이면 말하지 않고 계획만 기록 (첫 주)
REFLECT_MODEL=                # 비우면 BRAIN_MODEL
# 로컬 음성
TTS_ENGINE=supertonic
SUPERTONIC_VOICE=M1
SUPERTONIC_STEPS=4
STT_ENGINE=                   # whisper_server | groq | whisper (비우면 whisper_server 가 떠 있을 때 그것, 아니면 groq)
WHISPER_URL=http://127.0.0.1:18080/inference
```

## 11. 파일

| 파일 | 역할 | 크기 추정 |
|---|---|---|
| `voice/selfdb.py` | 스키마 생성, episode/ask/drive/memory/persona 읽고 쓰기. 다른 모듈은 전부 이걸 통해서만 DB 를 만진다 | 200줄 |
| `voice/initiative.py` | 추동 계산, 허용 필터, 발행, 장부 관찰(열기·닫기·분류), 정책 갱신. `--dry-run` | 300줄 |
| `voice/ledger_rules.py` | 응답 분류 키워드와 정책 상수. 테스트하기 쉽게 순수 함수만 | 60줄 |
| `voice/reflect.py` | 밤 반성 1회 실행. 입력 모으기 → claude -p → JSON 적용 | 150줄 |
| `voice/persona.py` | 인격 버전 show/approve/reject, 현재 텍스트 꺼내기 | 60줄 |
| `voice/daijin_mqtt.py` | episode 기록, 상태 구독, "지금 상태" 블록, supertonic/whisper_server 엔진 | +120줄 |
| `voice/systemd/daijin-initiative.service`, `daijin-reflect.service` + `.timer`, `whisper-server.service` | 서비스 | |
| `voice/tests/test_self_layer.py` | 아래 §12 | 300줄 |

## 12. 검증

- **단위**: 분류 규칙(문장 → 클래스), 허용 필터(각 조건을 하나씩 막아 보기), 간격 계산(backoff), 복구 문장 선택, 기억 대체(valid_to), 스키마 생성·재생성 멱등성. 전부 DB 를 임시 파일로.
- **재생**: 지난 1주 journald 의 🗣️/🤖/📢/📨 줄을 파싱해 `episodes`/`asks` 로 넣어 보는 스크립트. 장부가 과거 데이터에서 어떻게 분류하는지 눈으로 본다(예: 10-08 의 "말 좀 살살해, 인마" 는 `negative` 로 잡혀야 한다). 이게 첫 번째 현실 검증이다.
- **통합**: `test_device.py` 로 말을 걸어 episode 가 쌓이는지, initiative 를 `--dry-run` 으로 띄워 계획이 찍히는지, say.sh 로 말하게 한 뒤 10분 안에 말을 걸어 `engaged` 로 닫히는지.
- **음성**: 서버에서 supertonic 으로 한 문장 합성 → test_device 로 받아 되읽기. whisper_server 는 맥에서 test_device 로.

## 13. 순서 (PR 단위, 각각 esp32-iot 에서 먼저)

1. **supertonic 엔진** (서버, 반나절). 독립적이고 작고 효과가 바로 보인다. 보이스 프리셋 샘플을 만들어 고르게 한다.
2. **selfdb + 브레인의 episode 기록 + 상태 구독/블록** (하루). 이때부터 데이터가 쌓인다. 재생 스크립트로 지난주를 넣는다.
3. **initiative, dry-run** (하루). 일주일 돌려 보며 "말했을 것" 로그를 본다. 문턱과 간격을 여기서 맞춘다.
4. **initiative 켜기 + 장부 분류 + 정책** (반나절 + 관찰 2주).
5. **reflect + persona 승인** (하루).
6. **whisper_server 엔진** (맥에서 반나절). 홈서버로 옮길 때 켠다.

1 과 2 는 이번 주에 가능하다. 서버에는 10-08 커밋(턴 계측, 브레인 상태)이 아직 배포되지 않았으므로 2 를 올릴 때 함께 올린다.

## 14. 사용자가 정할 것

- 조용한 시간과 하루 예산의 기본값 (위는 23-08시, 3회로 가정).
- `contact` 추동을 v1 에 넣을지. "오늘 얘기 못 했네"가 가장 틀리기 쉬운 말 걸기다(유휴 기반 말 걸기는 사람들이 원한다는 근거가 가장 약하다). dry-run 으로 보고 정해도 된다.
- Supertonic 프리셋 목소리. 들어 보고.
- 인격 파일을 어디 둘지: DB 의 current 버전을 `voice/persona.local.md` 로도 써 두어 사람이 편집할 수 있게 할 것인지(제안: 그렇게 한다. 파일 편집 = human 제안 자동 승인).
