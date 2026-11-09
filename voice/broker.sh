#!/bin/bash
# console.sh 가 source 해서 쓰는 mosquitto_sub 인증 인자.
#   CLOUD_AUTH=(-h … -p … [--cafile …] -u … -P …)   우체국(secrets 의 MQTT_CLOUD_*)
#   SEC                                            저장소 루트의 secrets.local.txt (클론 위치 무관)
# 예전엔 로컬 mosquitto 가 떠 있으면 그쪽(다리 경유)을 고르는 AUTH/H/PORT 도 있었다. 다리를 걷으면서 쓰는 곳이 없어져 뺐다.
# 맥·리눅스 공통: CA 번들 위치를 순서대로 찾는다.
SEC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/secrets.local.txt"
_sec() { grep "^$1=" "$SEC" 2>/dev/null | cut -d= -f2-; }

CA=""
for _ca in /etc/ssl/cert.pem /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt; do
  if [ -f "$_ca" ]; then CA="$_ca"; break; fi          # macOS / Debian·Ubuntu / RHEL·Fedora
done
CLOUD_AUTH=(-h "$(_sec MQTT_CLOUD_HOST)" -p "$(_sec MQTT_CLOUD_PORT)")
if [ -n "$CA" ]; then CLOUD_AUTH+=(--cafile "$CA"); fi
CLOUD_AUTH+=(-u "$(_sec MQTT_CLOUD_USER)" -P "$(_sec MQTT_CLOUD_PASS)")
