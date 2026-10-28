// 15-home-node: daijin 홈 노드 공통 펌웨어 (HG ESP32-S3 DevKitC-1 N16R8용)
// 13-cloud-led 파생. 보드마다 NODE_NAME만 바꿔 플래시하면 같은 브로커에 여러 노드가 붙는다.
//
// 토픽 규약:
//   daijin/dev/<NODE_NAME>/cmd     (구독)  아래 명령표 참고
//   daijin/dev/all/cmd             (구독)  전 노드 브로드캐스트
//   daijin/dev/<NODE_NAME>/state   (발행, retained)  마지막으로 실행한 명령 ACK
//   daijin/dev/<NODE_NAME>/status  (발행, retained)  30초 하트비트 JSON + LWT로 offline 마킹
//
// 명령표 ("verb:arg" 형식, 동작이 끝난 뒤 ACK):
//   led:<색>              red green blue yellow cyan magenta white off
//   servo:<0~180>         절대 각도로 이동 (그 자리에 머묾)
//   poke:<각도>[:<ms>]    원샷: 각도로 갔다가 ms(기본 400) 뒤 원위치: 스위치 누르기, 풍선 터뜨리기
//   step:<±각도>          스텝모터(28BYJ-48)를 그 각도만큼 돌리고 멈춤 (음수=반대 방향, 느림: 180도≈3초)
//   motor[:<각도>]        노드가 뭐든 "모터 돌려": 서보 노드는 poke(기본 120도, 갔다 옴), 스텝 노드는 step(기본 180도). 0이면 기본값
//   relay:on|off          릴레이 (fan: 도 동의어)
//   relay:pulse[:<ms>]    ms(기본 500) 동안만 켬
//   buzz:<ms>             2kHz 비프 / buzz:<freq>:<ms> 주파수 지정
//   buzz:alarm | ok | fail  짧은 효과음
//   ping                  pong / caps  이 노드가 켜둔 기능 목록
//
// 네트워크: secrets.h에 WIFI_SSID(집)·WIFI_SSID2/3(아이폰 핫스팟)이 있으면 WiFiMulti로 잡히는
//   곳에 자동 접속(노드를 다른 장소로 옮겨도 재설정 불필요). 전부 실패하면 WiFiManager 포털
//   "daijin-<node>"로 폴백. 브로커는 클라우드(아웃바운드 TLS만)라 어느 망이든 같은 플릿에 붙는다.
//   ※ ESP32는 2.4GHz 전용: 아이폰 핫스팟은 "호환성 최대화"를 켜야 보인다.
//
// 명령은 수신 콜백에서 큐에 넣고 loop() 에서 온 순서대로 실행한다 (동작 중에 온 명령이 겹쳐 실행되지 않게).
//
// 장애 감지 설계: keepalive 15s + LWT(retained {"online":false}) → 전원이 뽑히면
// 브로커가 ~30초 내에 offline을 retained로 남긴다. 브레인은 fleet.sh로 조회.
//
// 온보드 RGB: DevKitC-1 v1.0=GPIO48, v1.1=GPIO38. 안 켜지면 RGB_PIN을 38로.
// 자격증명=secrets.h, CA=ca_cert.h (13-cloud-led와 동일 파일, gitignore됨).
//
// 보드별 플래시(파일 수정 없이 이름만 바꿔 컴파일):
//   arduino-cli compile --fqbn esp32:esp32:esp32s3 \
//     --build-property 'compiler.cpp.extra_flags=-DNODE_NAME="dev1"' 15-home-node

#include <WiFi.h>
#include <WiFiMulti.h>
#include <WiFiClientSecure.h>
#include <WiFiManager.h>          // tzapu/WiFiManager
#include <PubSubClient.h>
#include "secrets.h"              // MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASS (+ WIFI_SSID*)
#include "ca_cert.h"              // ISRG_ROOT_X1
#include "tls_time.h"             // 시계 맞추기: NTP, 막히면 HTTP Date (인증서 검증을 끄지 않기 위해)
#include "broker_choice.h"        // 클라우드에 못 붙으면 집 우체국으로 (secrets.h 에 MQTT_LOCAL_HOST 가 있을 때만)
#include "ota.h"                   // ota:<url>:<sha256> 명령: HTTPS 로 받아 검증하고 갈아 끼움 (USB 없이 갱신)

// 시각을 끝내 못 맞췄을 때 인증서 검증 없이라도 붙을지. 0(기본) = 붙지 않고 재부팅해서 다시 시도한다.
// 1 로 빌드하면 예전처럼 검증 없이 붙는다: 같은 망의 누구든 브로커 행세로 MQTT 비밀번호를 가로챌 수 있으니 급할 때만.
#ifndef ALLOW_INSECURE_TLS
#define ALLOW_INSECURE_TLS 0
#endif

// ---------- 보드별 설정 (플래시 전 여기만 바꾼다) ----------
#ifndef NODE_NAME
#define NODE_NAME  "dev2"         // dev1=거실, dev2=창가 ... 보드마다 고유하게
#endif
#define RGB_PIN    38             // HG DevkitC-1 V1.1 실크에 RGB/IO38 명시 (v1.0이면 48)

// ---------- 기능 스위치 ----------
// 배선한 것만 1. 꺼진 기능은 err:unknown으로 ACK해서 브레인이 "이 노드엔 없다"를 알게 한다.
// 노드별로는 flash-node.sh가 -DFEAT_*=0 플래그로 덮어쓴다 (dev1=서보·부저·릴레이, dev2=스텝모터·부저).
#ifndef FEAT_SERVO
#define FEAT_SERVO 1   // 킷 SG90: 신호=GPIO 4 (5V, GND)
#endif
#ifndef FEAT_RELAY
#define FEAT_RELAY 1   // 킷 알몸 릴레이: GPIO 5 → 1kΩ → S8050 베이스, 코일은 5V (다이오드 병렬)
#endif
#ifndef FEAT_STEPPER
#define FEAT_STEPPER 1   // 킷 28BYJ-48 + ULN2003: IN1..IN4 = GPIO 9,10,11,12 (보드 전원 5V, GND)
#endif
#ifndef FEAT_BUZZER
#define FEAT_BUZZER 1   // 킷 패시브 부저: +=GPIO 8, -=GND
#endif
#define FEAT_DHT    0             // 킷 DHT11: DATA=GPIO 6
#define FEAT_PIR    0             // 킷 PIR: OUT=GPIO 7 → 감지 시 event 발행
#define RELAY_ACTIVE_LOW 0        // 모듈이 LOW에서 켜지면 1

#if FEAT_SERVO
#include <ESP32Servo.h>
#ifndef NODE_COLOR
#define NODE_COLOR "green"        // 온라인 대기 색 = 노드 이름 색 (레드 노드=red, 블루 노드=blue). flash-node.sh 플래그
#endif
#ifndef SERVO_REST
#define SERVO_REST 0              // 부팅 시 서보 대기 각도 = poke의 시작점. 노드별 빌드 플래그로 지정
#endif
Servo servo; const int SERVO_PIN = 4; int servoPos = SERVO_REST;
#endif
#if FEAT_RELAY
const int RELAY_PIN = 5;
inline void relayWrite(bool on) { digitalWrite(RELAY_PIN, (on ^ RELAY_ACTIVE_LOW) ? HIGH : LOW); }
#endif
#if FEAT_BUZZER
const int BUZZER_PIN = 8;
#endif
#if FEAT_STEPPER
#include <Stepper.h>
#ifndef MOTOR_STEP_DEG
#define MOTOR_STEP_DEG 180        // motor 명령 기본 회전각. 노드별 빌드 플래그로 지정
#endif
const int STEPS_PER_REV = 2048;                   // 28BYJ-48 풀스텝(기어비 포함)
Stepper stepper(STEPS_PER_REV, 9, 11, 10, 12);   // Stepper 라이브러리 순서: IN1, IN3, IN2, IN4
#endif
#if FEAT_DHT
#include <DHT.h>
DHT dht(6, DHT11);
#endif
#if FEAT_PIR
const int PIR_PIN = 7; bool pirLast = false;
#endif

WiFiMulti        multi;
WiFiClientSecure net;
PubSubClient     mqtt(net);
BrokerChoice     broker;
WiFiManager      wm;
String clientId;

const char* T_CMD    = "daijin/dev/" NODE_NAME "/cmd";
const char* T_ALL    = "daijin/dev/all/cmd";
const char* T_STATE  = "daijin/dev/" NODE_NAME "/state";
const char* T_STATUS = "daijin/dev/" NODE_NAME "/status";

void setColor(const String& m) {
  if      (m=="off")     neopixelWrite(RGB_PIN,0,0,0);
  else if (m=="red")     neopixelWrite(RGB_PIN,40,0,0);
  else if (m=="blue")    neopixelWrite(RGB_PIN,0,0,40);
  else if (m=="yellow")  neopixelWrite(RGB_PIN,40,40,0);
  else if (m=="magenta") neopixelWrite(RGB_PIN,40,0,40);
  else if (m=="cyan")    neopixelWrite(RGB_PIN,0,40,40);
  else if (m=="white")   neopixelWrite(RGB_PIN,40,40,40);
  else                   neopixelWrite(RGB_PIN,0,40,0);  // green/그외
}

void ack(const String& what) { mqtt.publish(T_STATE, what.c_str(), true); }

// 동작 중에도 브로커 keepalive가 끊기지 않도록 잘게 쪼개서 기다린다
void holdMs(uint32_t ms) {
  uint32_t t0 = millis();
  while (millis() - t0 < ms) { mqtt.loop(); delay(10); }
}

#if FEAT_STEPPER
void stepperRelease() { for (int p : {9, 10, 11, 12}) digitalWrite(p, LOW); }  // 코일 끊어 발열·전류 절감
void stepDegrees(int deg) {                       // 32스텝씩 끊어 돌리며 MQTT keepalive 유지
  long n = (long)deg * STEPS_PER_REV / 360;
  int dir = n < 0 ? -1 : 1; n = labs(n);
  while (n > 0) { int k = n > 32 ? 32 : (int)n; stepper.step(dir * k); n -= k; mqtt.loop(); }
  stepperRelease();
}
#endif

#if FEAT_BUZZER
void beep(unsigned freq, uint32_t ms) { tone(BUZZER_PIN, freq); holdMs(ms); noTone(BUZZER_PIN); }
void buzzPreset(const String& name) {
  if (name == "alarm")      { for (int i = 0; i < 3; i++) { beep(1800, 180); beep(1200, 180); } }
  else if (name == "ok")    { beep(660, 90); beep(990, 140); }
  else if (name == "fail")  { beep(400, 250); holdMs(60); beep(300, 350); }
  else                      { beep(2000, constrain(name.toInt(), 20, 3000)); }   // buzz:<ms>
}
#endif

String caps() {
  String s = "caps:led,ping,motor";
#if FEAT_SERVO
  s += ",servo,poke";
#endif
#if FEAT_RELAY
  s += ",relay,fan";
#endif
#if FEAT_BUZZER
  s += ",buzz";
#endif
#if FEAT_STEPPER
  s += ",step";
#endif
#if FEAT_DHT
  s += ",dht";
#endif
#if FEAT_PIR
  s += ",pir";
#endif
  return s;
}

bool cmdFromAll = false;              // 지금 실행 중인 명령이 all/cmd 로 왔나 (runQueued 가 정한다; ota 는 거절)

// "verb:arg[:arg2]" 명령 실행. 모르는 명령은 state에 "err:..."로 남겨 브레인이 알 수 있게.
void runCmd(const String& cmd) {
  int c = cmd.indexOf(':');
  String verb = c < 0 ? cmd : cmd.substring(0, c);
  String rest = c < 0 ? ""  : cmd.substring(c + 1);
  int c2 = rest.indexOf(':');
  String arg  = c2 < 0 ? rest : rest.substring(0, c2);
  String arg2 = c2 < 0 ? ""   : rest.substring(c2 + 1);

  if (verb == "led")        { setColor(arg); ack(cmd); }
  else if (verb == "ping")  { ack("pong"); }
  else if (verb == "caps")  { ack(caps()); }
  else if (verb == "motor") {                                    // 범용: 달린 모터 종류에 맞춰 눈에 보이게 움직임
#if FEAT_SERVO
    int a = arg.toInt() > 0 ? constrain(arg.toInt(), 1, 180) : 120;   // 0이나 빈 값은 기본 120 (안 움직이는 명령 방지)
    servo.write(a); holdMs(600); servo.write(servoPos); holdMs(300);
    ack("motor:servo-poke:" + String(a));
#elif FEAT_STEPPER
    int d = arg.toInt() != 0 ? constrain(arg.toInt(), -1080, 1080) : MOTOR_STEP_DEG; // 0이나 빈 값은 기본값
    stepDegrees(d);
    ack("motor:step:" + String(d));
#else
    ack("err:unknown:" + cmd);
#endif
  }
#if FEAT_SERVO
  else if (verb == "servo") {
    int a = constrain(arg.toInt(), 0, 180);
    bool same = (a == servoPos);                                 // 이미 그 각도면 눈에 안 보인다 → ACK에 표시
    servoPos = a; servo.write(servoPos); holdMs(100);   // ACK 전 대기 최소화
    ack(same ? cmd + ":nochange" : cmd);
  }
  else if (verb == "poke")  {                                     // 갔다가 돌아오는 원샷
    int a = constrain(arg.toInt(), 0, 180);
    uint32_t hold = arg2.length() ? constrain(arg2.toInt(), 50, 3000) : 400;
    servo.write(a); holdMs(hold); servo.write(servoPos); holdMs(300);
    ack(cmd);
  }
#endif
#if FEAT_RELAY
  else if (verb == "relay" || verb == "fan") {
    if (arg == "pulse") {
      uint32_t ms = arg2.length() ? constrain(arg2.toInt(), 50, 3000) : 500;
      relayWrite(true); holdMs(ms); relayWrite(false);
    } else relayWrite(arg == "on");
    ack(cmd);
  }
#endif
#if FEAT_STEPPER
  else if (verb == "step")  { stepDegrees(constrain(arg.toInt(), -1080, 1080)); ack(cmd); }
#endif
#if FEAT_BUZZER
  else if (verb == "buzz") {
    if (arg2.length()) beep(constrain(arg.toInt(), 100, 8000), constrain(arg2.toInt(), 20, 3000));  // buzz:<freq>:<ms>
    else buzzPreset(arg);
    ack(cmd);
  }
#endif
  else if (verb == "ota") {                                       // ota:<url>:<sha256> — 자기 토픽으로 온 것만 (all/cmd 는 보드마다 다른 바이너리라 거절)
    if (cmdFromAll) { ack("err:ota-via-all"); return; }
    ack("ota:start");                                              // 이 뒤로는 MQTT 를 끊는다: 결과는 재부팅 뒤 status 의 fw, 실패는 다시 붙은 뒤 state
    mqtt.loop(); delay(100); mqtt.disconnect(); net.stop();
    otaCommand(cmd, ISRG_ROOT_X1);                                 // 성공하면 여기서 재부팅한다
    Serial.println(otaResult);
  }
  else                      { ack("err:unknown:" + cmd); }
}

// 명령 큐: 수신 콜백은 넣기만 하고, 실행은 loop() 가 한다.
// 예전엔 콜백 안에서 바로 실행했는데, 동작 중에 keepalive 를 위해 mqtt.loop() 를 다시 부르기 때문에
// 그 사이 도착한 명령이 콜백 안의 콜백으로 겹쳐 실행됐다 (모터가 도는 중에 다른 모터 명령이 끼어듦).
// 이제 동작 중에 온 명령은 줄을 서고, 줄이 꽉 차면 err:busy 로 알린다.
const int QLEN = 4;
String cmdQ[QLEN]; bool cmdQAll[QLEN]; int qHead = 0, qCount = 0;

void onMessage(char* t, byte* p, unsigned int n) {
  String m; m.reserve(n); for (unsigned int i = 0; i < n; i++) m += (char)p[i];
  Serial.printf("[MQTT] %s <- %s\n", t, m.c_str());
  if (qCount == QLEN) { ack("err:busy:" + m); return; }
  int slot = (qHead + qCount++) % QLEN;
  cmdQ[slot] = m; cmdQAll[slot] = (strcmp(t, T_ALL) == 0);
}

void runQueued() {                    // loop() 에서 호출: 줄 선 명령을 하나씩, 온 순서대로
  while (qCount > 0) {
    String m = cmdQ[qHead]; cmdQ[qHead] = ""; cmdFromAll = cmdQAll[qHead];
    qHead = (qHead + 1) % QLEN; qCount--;
    runCmd(m);
  }
}

// 30초 하트비트: retained라 브레인이 언제든 마지막 상태를 읽는다.
// ts=0이면 NTP 미동기(온라인 판정은 LWT의 online 필드로만). ssid로 어느 망에 붙었는지 보인다.
uint32_t lastBeat = 0;
void heartbeat() {
  if (millis() - lastBeat < 30000 && lastBeat != 0) return;
  lastBeat = millis();
  char js[300];
  float temp = temperatureRead();
#if FEAT_DHT
  float rt = dht.readTemperature(), rh = dht.readHumidity();
  snprintf(js, sizeof(js),
    "{\"online\":true,\"node\":\"%s\",\"ts\":%ld,\"up_s\":%lu,\"rssi\":%d,\"chip_c\":%.1f,\"ssid\":\"%s\",\"room_c\":%.1f,\"room_rh\":%.0f,\"heap\":%u,\"broker\":\"%s\",\"fw\":\"%s\"}",
    NODE_NAME, (long)time(nullptr), millis()/1000UL, WiFi.RSSI(), temp, WiFi.SSID().c_str(),
    isnan(rt) ? -1 : rt, isnan(rh) ? -1 : rh, ESP.getFreeHeap(), broker.name(), FW_VERSION);
#else
  snprintf(js, sizeof(js),
    "{\"online\":true,\"node\":\"%s\",\"ts\":%ld,\"up_s\":%lu,\"rssi\":%d,\"chip_c\":%.1f,\"ssid\":\"%s\",\"heap\":%u,\"broker\":\"%s\",\"fw\":\"%s\"}",
    NODE_NAME, (long)time(nullptr), millis()/1000UL, WiFi.RSSI(), temp, WiFi.SSID().c_str(), ESP.getFreeHeap(), broker.name(), FW_VERSION);
#endif
  mqtt.publish(T_STATUS, (const uint8_t*)js, strlen(js), true);
}

// 1차: secrets.h의 알려진 망(집/핫스팟) 중 잡히는 곳: 노드를 어디로 옮겨도 그냥 켜면 붙는다
bool connectKnownWiFi(uint32_t budgetMs) {
#ifdef WIFI_SSID
  WiFi.mode(WIFI_STA);
  uint32_t t0 = millis();
  while (millis() - t0 < budgetMs) {
    neopixelWrite(RGB_PIN, 0, 0, 20);
    if (multi.run(7000) == WL_CONNECTED) return true;
    neopixelWrite(RGB_PIN, 0, 0, 0); delay(300);
  }
#endif
  return false;
}

void setup() {
  Serial.begin(115200); delay(200);
  clientId = String("node-") + NODE_NAME + "-" + String((uint32_t)(ESP.getEfuseMac() & 0xFFFFFF), HEX);
  neopixelWrite(RGB_PIN, 8, 8, 8);                 // 부팅 = 흰색 약하게

#if FEAT_SERVO
  servo.attach(SERVO_PIN); servo.write(servoPos);
#endif
#if FEAT_RELAY
  pinMode(RELAY_PIN, OUTPUT); relayWrite(false);
#endif
#if FEAT_BUZZER
  pinMode(BUZZER_PIN, OUTPUT); noTone(BUZZER_PIN);
#endif
#if FEAT_STEPPER
  stepper.setSpeed(12);                            // rpm. 28BYJ-48은 15 넘기면 탈조
  stepperRelease();
#endif
#if FEAT_DHT
  dht.begin();
#endif
#if FEAT_PIR
  pinMode(PIR_PIN, INPUT);
#endif

  // WIFI_ONLY=1|2|3 빌드 플래그가 있으면 그 망만 등록 (촬영·테스트용: 집 와이파이가 더 세도 핫스팟에 붙게).
  // 없으면 아는 망 전부 등록하고 WiFiMulti가 신호 센 곳을 고른다.
#if defined(WIFI_SSID) && (!defined(WIFI_ONLY) || WIFI_ONLY == 1)
  multi.addAP(WIFI_SSID, WIFI_PASS);               // 집
#endif
#if defined(WIFI_SSID2) && (!defined(WIFI_ONLY) || WIFI_ONLY == 2)
  multi.addAP(WIFI_SSID2, WIFI_PASS2);             // 아이폰 핫스팟 1
#endif
#if defined(WIFI_SSID3) && (!defined(WIFI_ONLY) || WIFI_ONLY == 3)
  multi.addAP(WIFI_SSID3, WIFI_PASS3);             // 아이폰 핫스팟 2
#endif

  if (!connectKnownWiFi(25000)) {
    // 2차: 현장 WiFi 프로비저닝: AP "daijin-<node>" 포털 (보라 = 설정 대기)
    wm.setAPCallback([](WiFiManager*){ neopixelWrite(RGB_PIN, 30, 0, 30); });
    wm.setConfigPortalTimeout(300);
    if (!wm.autoConnect(("daijin-" NODE_NAME))) { ESP.restart(); }
  }
  WiFi.setSleep(false);                            // 핫스팟에서 응답 지연·끊김 방지
  Serial.printf("WiFi OK: %s (%s)\n", WiFi.localIP().toString().c_str(), WiFi.SSID().c_str());

  // 시계가 맞아야 인증서를 검증할 수 있다 (하트비트 타임스탬프도 이 시계). NTP → HTTP Date 순 (tls_time.h).
  // 집 우체국이 설정돼 있으면 시각을 못 얻어도 재부팅하지 않고 집 우체국으로 간다 (인터넷이 끊긴 것, broker_choice.h).
  if (syncClock()) { broker.begin(net, mqtt, true); Serial.printf("time OK (%s) → CA 검증\n", clockSource); }
  else if (broker.enabled()) { broker.begin(net, mqtt, false); Serial.println("시각 동기 실패 → 집 우체국으로 시작 (5분마다 클라우드 재시도)"); }
  else {
    broker.begin(net, mqtt, false);
#if ALLOW_INSECURE_TLS
    net.setInsecure(); Serial.println("시각 동기 실패 → setInsecure (ALLOW_INSECURE_TLS=1)");
#else
    Serial.println("시각 동기 실패 → 검증을 끄지 않고 10초 뒤 재부팅해서 다시 시도");
    for (int i = 0; i < 10; i++) { neopixelWrite(RGB_PIN, 40, 0, 40); delay(300); neopixelWrite(RGB_PIN, 0, 0, 0); delay(700); }
    ESP.restart();
#endif
  }

  mqtt.setCallback(onMessage);
  mqtt.setKeepAlive(15);                           // 죽음을 ~30초 내 감지 (LWT)
  mqtt.setSocketTimeout(30);
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {             // 핫스팟이 잠들었다 깨는 경우 등: 재접속
    Serial.println("WiFi 끊김 → 재접속");
    if (!connectKnownWiFi(20000)) ESP.restart();
  }
  if (!mqtt.connected()) {
    neopixelWrite(RGB_PIN, 40, 40, 0);             // 노랑 = 브로커 연결 시도 (빨강은 레드 노드 이름 색이라 피함)
    if (broker.choose()) Serial.printf("우체국 바꿈 → %s\n", broker.name());
    Serial.printf("MQTT 연결 (%s)...", broker.name());
    // LWT: 연결이 끊기면 브로커가 status에 offline을 retained로 남긴다
    String will = String("{\"online\":false,\"node\":\"" NODE_NAME "\"}");
    bool ok = mqtt.connect(clientId.c_str(), broker.user(), broker.pass(), T_STATUS, 1, true, will.c_str());
    ok = broker.result(ok);
    if (ok) {
      Serial.println(" OK");
      mqtt.subscribe(T_CMD);
      mqtt.subscribe(T_ALL);
      setColor(NODE_COLOR);                        // 온라인 = 노드 이름 색
      lastBeat = 0; heartbeat();                   // 접속 즉시 online 하트비트
      if (otaResult.length()) { ack(otaResult); otaResult = ""; }   // 실패한 OTA 가 있었으면 이제야 알린다
#if FEAT_BUZZER
      buzzPreset("ok");                            // 촬영 시 "붙었다" 신호
#endif
    } else {
      Serial.printf(" 실패(rc=%d)\n", mqtt.state());
      delay(3000);
    }
  }
  mqtt.loop();
  runQueued();
  heartbeat();

#if FEAT_PIR
  bool pir = digitalRead(PIR_PIN);
  if (pir && !pirLast) {                           // 상승 에지에서만 이벤트 발행
    char ev[96];
    snprintf(ev, sizeof(ev), "{\"node\":\"%s\",\"event\":\"motion\",\"ts\":%ld}",
             NODE_NAME, (long)time(nullptr));
    mqtt.publish("daijin/dev/" NODE_NAME "/event", ev);
  }
  pirLast = pir;
#endif
}
