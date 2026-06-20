// 10-remote-led — 원격으로 LED 색 점등/끄기 (HTTP + curl, LAN)
// 터미널 사용법:
//   curl http://esp32.local/to_red      빨강 점등
//   curl http://esp32.local/to_green    초록
//   curl http://esp32.local/to_blue     파랑
//   curl http://esp32.local/to_off      끄기
//   사용 가능 색: red green blue yellow cyan magenta white orange off
//   curl http://esp32.local/            도움말/현재 사용법 보기
// ⚠️ WIFI_SSID / WIFI_PASS 는 본인 2.4GHz WiFi (5GHz 안 됨)

#include <WiFi.h>
#include <WebServer.h>
#include <ESPmDNS.h>

const char* WIFI_SSID = "YOUR_WIFI_2.4GHz";    // ← 본인 2.4GHz WiFi 이름
const char* WIFI_PASS = "YOUR_WIFI_PASSWORD";  // ← 본인 비번 (이후 스케치는 secrets.h로 분리)

#define RGB_PIN 48
WebServer server(80);

struct Color { const char* name; uint8_t r, g, b; };
Color COLORS[] = {
  {"red",     40,  0,  0},
  {"green",    0, 40,  0},
  {"blue",     0,  0, 40},
  {"yellow",  40, 40,  0},
  {"cyan",     0, 40, 40},
  {"magenta", 40,  0, 40},
  {"white",   40, 40, 40},
  {"orange",  40, 14,  0},
  {"off",      0,  0,  0},
};
const int NCOLORS = sizeof(COLORS) / sizeof(COLORS[0]);

String lastColor = "off";

void handleRequest() {
  String u = server.uri();                 // 예: /to_green
  if (u.startsWith("/to_")) {
    String name = u.substring(4);          // "green"
    for (int i = 0; i < NCOLORS; i++) {
      if (name == COLORS[i].name) {
        neopixelWrite(RGB_PIN, COLORS[i].r, COLORS[i].g, COLORS[i].b);
        lastColor = name;
        server.send(200, "text/plain", "OK -> " + name + "\n");
        return;
      }
    }
    server.send(404, "text/plain", "unknown color: " + name +
                "\n(use: red green blue yellow cyan magenta white orange off)\n");
    return;
  }
  // 그 외 (루트 등) → 사용법
  String s = "ESP32 remote LED\n";
  s += "current: " + lastColor + "\n";
  s += "usage: curl http://esp32.local/to_<color>\n";
  s += "colors: red green blue yellow cyan magenta white orange off\n";
  server.send(200, "text/plain", s);
}

void setup() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) {   // 연결 대기 = 하양 깜빡
    neopixelWrite(RGB_PIN, 8, 8, 8); delay(150);
    neopixelWrite(RGB_PIN, 0, 0, 0); delay(150);
  }
  MDNS.begin("esp32");
  MDNS.addService("http", "tcp", 80);
  server.onNotFound(handleRequest);         // /to_* 등 모든 경로 처리
  server.on("/", handleRequest);
  server.begin();
  neopixelWrite(RGB_PIN, 0, 40, 0);         // 연결됨 표시 = 초록 점등
  lastColor = "green";
}

void loop() {
  server.handleClient();                    // 명령 처리. 색은 setWrite로 유지됨(점멸 없음)
}
