// 02-wifi-test — 통신 검증 (사용자 1순위 요구)
// Wi-Fi 접속 → IP 출력 → 간단 HTTP 서버. 폰 브라우저로 보드 IP 접속 시 상태 페이지.
//
// 쓰기 전에: 아래 SSID / PASSWORD 본인 와이파이로 수정.
// 업로드 후 시리얼 모니터(115200)에 IP가 찍히면 통신 검증 완료.

#include <WiFi.h>
#include <WebServer.h>

const char* SSID     = "YOUR_WIFI";       // ← 수정
const char* PASSWORD = "YOUR_PASSWORD";   // ← 수정

WebServer server(80);

void handleRoot() {
  String html = "<h1>ESP32-S3 살아있음 ✅</h1>";
  html += "<p>IP: " + WiFi.localIP().toString() + "</p>";
  html += "<p>RSSI: " + String(WiFi.RSSI()) + " dBm</p>";
  html += "<p>uptime: " + String(millis() / 1000) + " s</p>";
  server.send(200, "text/html; charset=utf-8", html);
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.printf("\n[02-wifi-test] connecting to %s ...\n", SSID);

  WiFi.mode(WIFI_STA);
  WiFi.begin(SSID, PASSWORD);
  while (WiFi.status() != WL_CONNECTED) {
    delay(400);
    Serial.print(".");
  }
  Serial.println();
  Serial.print("✅ 통신 검증 완료 — IP: ");
  Serial.println(WiFi.localIP());
  Serial.println("→ 폰/PC 브라우저에서 위 IP로 접속해보세요.");

  server.on("/", handleRoot);
  server.begin();
}

void loop() {
  server.handleClient();
}
