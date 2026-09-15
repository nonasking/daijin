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
Servo servo; const int SERVO_PIN = 4; int servoPos = 0;
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
    int d = arg.toInt() != 0 ? constrain(arg.toInt(), -1080, 1080) : 180; // 0이나 빈 값은 기본 180
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
  else                      { ack("err:unknown:" + cmd); }
}

void onMessage(char* t, byte* p, unsigned int n) {
  String m; for (unsigned int i = 0; i < n; i++) m += (char)p[i];
  Serial.printf("[MQTT] %s <- %s\n", t, m.c_str());
  runCmd(m);
}

bool syncTime(uint32_t timeoutMs) {   // TLS 인증서 검증 + 하트비트 타임스탬프용
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
  uint32_t start = millis(); time_t now = time(nullptr);
  while (now < 1700000000 && millis() - start < timeoutMs) { delay(200); now = time(nullptr); }
  return now >= 1700000000;
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
    "{\"online\":true,\"node\":\"%s\",\"ts\":%ld,\"up_s\":%lu,\"rssi\":%d,\"chip_c\":%.1f,\"ssid\":\"%s\",\"room_c\":%.1f,\"room_rh\":%.0f,\"heap\":%u}",
    NODE_NAME, (long)time(nullptr), millis()/1000UL, WiFi.RSSI(), temp, WiFi.SSID().c_str(),
    isnan(rt) ? -1 : rt, isnan(rh) ? -1 : rh, ESP.getFreeHeap());
#else
  snprintf(js, sizeof(js),
    "{\"online\":true,\"node\":\"%s\",\"ts\":%ld,\"up_s\":%lu,\"rssi\":%d,\"chip_c\":%.1f,\"ssid\":\"%s\",\"heap\":%u}",
    NODE_NAME, (long)time(nullptr), millis()/1000UL, WiFi.RSSI(), temp, WiFi.SSID().c_str(), ESP.getFreeHeap());
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

  if (syncTime(12000)) { net.setCACert(ISRG_ROOT_X1); Serial.println("time OK → CA 검증"); }
  else { net.setInsecure(); Serial.println("NTP 실패 → setInsecure(암호화만)"); }

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
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
    neopixelWrite(RGB_PIN, 40, 0, 0);              // 빨강 = 브로커 연결 시도
    Serial.print("MQTT 연결...");
    // LWT: 연결이 끊기면 브로커가 status에 offline을 retained로 남긴다
    String will = String("{\"online\":false,\"node\":\"" NODE_NAME "\"}");
    if (mqtt.connect(clientId.c_str(), MQTT_USER, MQTT_PASS,
                     T_STATUS, 1, true, will.c_str())) {
      Serial.println(" OK");
      mqtt.subscribe(T_CMD);
      mqtt.subscribe(T_ALL);
      neopixelWrite(RGB_PIN, 0, 40, 0);            // 초록 = 온라인
      lastBeat = 0; heartbeat();                   // 접속 즉시 online 하트비트
#if FEAT_BUZZER
      buzzPreset("ok");                            // 촬영 시 "붙었다" 신호
#endif
    } else {
      Serial.printf(" 실패(rc=%d)\n", mqtt.state());
      delay(3000);
    }
  }
  mqtt.loop();
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
