// 15-home-node — daijin 홈 노드 공통 펌웨어 (HG ESP32-S3 DevKitC-1 N16R8용)
// 13-cloud-led 파생. 보드마다 NODE_NAME만 바꿔 플래시하면 같은 브로커에 여러 노드가 붙는다.
//
// 토픽 규약:
//   daijin/dev/<NODE_NAME>/cmd     (구독)  "led:red" "servo:90" "relay:on" "fan:on" ...
//   daijin/dev/all/cmd             (구독)  전 노드 브로드캐스트
//   daijin/dev/<NODE_NAME>/state   (발행, retained)  마지막으로 실행한 명령 ACK
//   daijin/dev/<NODE_NAME>/status  (발행, retained)  30초 하트비트 JSON + LWT로 offline 마킹
//
// 장애 감지 설계: keepalive 15s + LWT(retained {"online":false}) → 전원이 뽑히면
// 브로커가 ~30초 내에 offline을 retained로 남긴다. 브레인은 fleet.sh로 조회.
//
// 온보드 RGB: DevKitC-1 v1.0=GPIO48, v1.1=GPIO38. 안 켜지면 RGB_PIN을 38로.
// 자격증명=secrets.h, CA=ca_cert.h (13-cloud-led와 동일 파일, gitignore됨).

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <WiFiManager.h>          // tzapu/WiFiManager
#include <PubSubClient.h>
#include "secrets.h"              // MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASS
#include "ca_cert.h"              // ISRG_ROOT_X1

// ---------- 보드별 설정 (플래시 전 여기만 바꾼다) ----------
#define NODE_NAME  "dev2"         // dev1=거실, dev2=창가 ... 보드마다 고유하게
#define RGB_PIN    38             // HG DevkitC-1 V1.1 실크에 RGB/IO38 명시 (v1.0이면 48)

// ---------- 기능 스위치 (시나리오 확정 후 1로) ----------
#define FEAT_SERVO 0              // 킷 SG90: 신호=GPIO 4 (5V, GND)
#define FEAT_RELAY 0              // 킷 릴레이: IN=GPIO 5
#define FEAT_DHT   0              // 킷 DHT11: DATA=GPIO 6
#define FEAT_PIR   0              // 킷 PIR: OUT=GPIO 7 → 감지 시 event 발행

#if FEAT_SERVO
#include <ESP32Servo.h>
Servo servo; const int SERVO_PIN = 4;
#endif
#if FEAT_RELAY
const int RELAY_PIN = 5;
#endif
#if FEAT_DHT
#include <DHT.h>
DHT dht(6, DHT11);
#endif
#if FEAT_PIR
const int PIR_PIN = 7; bool pirLast = false;
#endif

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

// "verb:arg" 명령 실행. 모르는 명령은 state에 "err:..."로 남겨 브레인이 알 수 있게.
void runCmd(const String& cmd) {
  int c = cmd.indexOf(':');
  String verb = c < 0 ? cmd : cmd.substring(0, c);
  String arg  = c < 0 ? ""  : cmd.substring(c + 1);

  if (verb == "led")        { setColor(arg); ack(cmd); }
  else if (verb == "ping")  { ack("pong"); }
#if FEAT_SERVO
  else if (verb == "servo") { int a = constrain(arg.toInt(), 0, 180); servo.write(a); ack(cmd); }
#endif
#if FEAT_RELAY
  else if (verb == "relay" || verb == "fan")
                            { digitalWrite(RELAY_PIN, arg == "on" ? HIGH : LOW); ack(cmd); }
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

// 30초 하트비트 — retained라 브레인이 언제든 마지막 상태를 읽는다.
// ts=0이면 NTP 미동기(온라인 판정은 LWT의 online 필드로만).
uint32_t lastBeat = 0;
void heartbeat() {
  if (millis() - lastBeat < 30000 && lastBeat != 0) return;
  lastBeat = millis();
  char js[240];
  float temp = temperatureRead();
#if FEAT_DHT
  float rt = dht.readTemperature(), rh = dht.readHumidity();
  snprintf(js, sizeof(js),
    "{\"online\":true,\"node\":\"%s\",\"ts\":%ld,\"up_s\":%lu,\"rssi\":%d,\"chip_c\":%.1f,\"room_c\":%.1f,\"room_rh\":%.0f,\"heap\":%u}",
    NODE_NAME, (long)time(nullptr), millis()/1000UL, WiFi.RSSI(), temp,
    isnan(rt) ? -1 : rt, isnan(rh) ? -1 : rh, ESP.getFreeHeap());
#else
  snprintf(js, sizeof(js),
    "{\"online\":true,\"node\":\"%s\",\"ts\":%ld,\"up_s\":%lu,\"rssi\":%d,\"chip_c\":%.1f,\"heap\":%u}",
    NODE_NAME, (long)time(nullptr), millis()/1000UL, WiFi.RSSI(), temp, ESP.getFreeHeap());
#endif
  mqtt.publish(T_STATUS, (const uint8_t*)js, strlen(js), true);
}

void setup() {
  Serial.begin(115200); delay(200);
  clientId = String("node-") + NODE_NAME + "-" + String((uint32_t)(ESP.getEfuseMac() & 0xFFFFFF), HEX);
  neopixelWrite(RGB_PIN, 8, 8, 8);                 // 부팅 = 흰색 약하게

#if FEAT_SERVO
  servo.attach(SERVO_PIN);
#endif
#if FEAT_RELAY
  pinMode(RELAY_PIN, OUTPUT); digitalWrite(RELAY_PIN, LOW);
#endif
#if FEAT_DHT
  dht.begin();
#endif
#if FEAT_PIR
  pinMode(PIR_PIN, INPUT);
#endif

  // 현장 WiFi 프로비저닝 — 저장된 WiFi 없으면 AP "daijin-<node>" 포털
  wm.setAPCallback([](WiFiManager*){ neopixelWrite(RGB_PIN, 30, 0, 30); }); // 보라 = 설정 대기
  wm.setConfigPortalTimeout(300);
  if (!wm.autoConnect(("daijin-" NODE_NAME))) { ESP.restart(); }
  Serial.printf("WiFi OK: %s\n", WiFi.localIP().toString().c_str());

  if (syncTime(12000)) { net.setCACert(ISRG_ROOT_X1); Serial.println("time OK → CA 검증"); }
  else { net.setInsecure(); Serial.println("NTP 실패 → setInsecure(암호화만)"); }

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMessage);
  mqtt.setKeepAlive(15);                           // 죽음을 ~30초 내 감지 (LWT)
  mqtt.setSocketTimeout(30);
}

void loop() {
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
