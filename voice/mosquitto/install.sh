#!/bin/bash
# VPS 에 다이진 우체국(Mosquitto + Let's Encrypt + 기기별 사용자) 을 올린다. 서버에서 root 로:
#   sudo MQTT_DOMAIN=mqtt.example.com bash ~/esp32-iot/voice/mosquitto/install.sh
# 두 번 돌려도 안전하다(설정은 덮어쓰고, 있는 사용자는 비밀번호를 바꾸지 않는다). 전제:
#   - MQTT_DOMAIN 의 A 레코드가 이 서버를 가리킨다 (certbot standalone 이 80 포트로 확인한다)
#   - 80, 8883 이 밖에서 열려 있다 (Hetzner 방화벽을 쓰면 거기서도)
# 끝나면 brain/device/nodes 비밀번호를 한 번 찍어 준다. 넣는 곳:
#   브레인: secrets.local.txt 의 MQTT_CLOUD_HOST=$MQTT_DOMAIN, MQTT_CLOUD_USER=brain, MQTT_CLOUD_PASS=…
#   디바이스·노드: sketches/*/secrets.h 의 MQTT_HOST/MQTT_USER/MQTT_PASS, 다시 플래시
set -e
[ "$(id -u)" = 0 ] || { echo "root 로 (sudo) 돌릴 것"; exit 1; }
: "${MQTT_DOMAIN:?MQTT_DOMAIN=mqtt.example.com 이 필요하다}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq mosquitto mosquitto-clients certbot >/dev/null
systemctl stop mosquitto 2>/dev/null || true

# 인증서: 처음이면 standalone 으로 받고, 갱신 때마다 mosquitto 가 읽을 수 있는 자리로 복사하는 hook 을 둔다
HOOK=/etc/letsencrypt/renewal-hooks/deploy/mosquitto-certs.sh
mkdir -p "$(dirname "$HOOK")" /etc/mosquitto/certs
cat > "$HOOK" <<HOOKEOF
#!/bin/bash
# certbot 이 갱신한 인증서를 mosquitto 사용자가 읽을 수 있게 복사하고 다시 읽힌다
set -e
D=/etc/letsencrypt/live/$MQTT_DOMAIN
cp "\$D/fullchain.pem" "\$D/privkey.pem" /etc/mosquitto/certs/
chown mosquitto:mosquitto /etc/mosquitto/certs/*.pem; chmod 600 /etc/mosquitto/certs/privkey.pem
systemctl reload mosquitto 2>/dev/null || systemctl restart mosquitto
HOOKEOF
chmod +x "$HOOK"
if [ ! -d "/etc/letsencrypt/live/$MQTT_DOMAIN" ]; then
  certbot certonly --standalone --non-interactive --agree-tos --register-unsafely-without-email -d "$MQTT_DOMAIN"
fi
cp "/etc/letsencrypt/live/$MQTT_DOMAIN/fullchain.pem" "/etc/letsencrypt/live/$MQTT_DOMAIN/privkey.pem" /etc/mosquitto/certs/
chown mosquitto:mosquitto /etc/mosquitto/certs/*.pem; chmod 600 /etc/mosquitto/certs/privkey.pem

# 설정과 ACL
install -m 644 "$HERE/mosquitto.conf" /etc/mosquitto/conf.d/daijin.conf
install -m 644 "$HERE/daijin.acl" /etc/mosquitto/daijin.acl
rm -f /etc/mosquitto/conf.d/default.conf 2>/dev/null || true

# 사용자 셋: 있으면 그대로, 없으면 비밀번호를 만들어 한 번 보여 준다
# 비밀번호 파일은 root 소유(mosquitto 2.x 가 요구), mosquitto 가 읽을 수 있게 그룹만 준다.
# 새 비밀번호는 만든 즉시 출력한다: 뒤에서 무엇이 실패해도 해시만 남고 원문을 잃는 일이 없게.
touch /etc/mosquitto/daijin.passwd; chown root:mosquitto /etc/mosquitto/daijin.passwd; chmod 640 /etc/mosquitto/daijin.passwd
for u in brain device nodes; do
  if ! grep -q "^$u:" /etc/mosquitto/daijin.passwd; then
    p="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 28)"
    mosquitto_passwd -b /etc/mosquitto/daijin.passwd "$u" "$p"
    echo "새 사용자 (이 줄은 다시 안 나온다, 비밀번호 관리자에 적을 것):  $u = $p"
  fi
done
chown root:mosquitto /etc/mosquitto/daijin.passwd; chmod 640 /etc/mosquitto/daijin.passwd   # mosquitto_passwd 가 다시 쓴 뒤

systemctl enable --now mosquitto
sleep 1
systemctl is-active mosquitto >/dev/null || { echo "mosquitto 가 뜨지 않았다: journalctl -u mosquitto"; exit 1; }
command -v ufw >/dev/null && ufw status | grep -q "^Status: active" && ufw allow 8883/tcp >/dev/null && ufw allow 80/tcp >/dev/null

echo "우체국 준비: $MQTT_DOMAIN:8883 (TLS), 127.0.0.1:1883 (브레인용 평문)"
echo "확인: mosquitto_sub -h $MQTT_DOMAIN -p 8883 -u brain -P '…' -t 'daijin/#' -v"
