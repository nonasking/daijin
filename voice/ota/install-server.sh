#!/bin/bash
# VPS 에 펌웨어 내려받기 자리(nginx, HTTPS)를 만든다. 보드는 여기서 .bin 을 받는다 (sketches/ota.h). 서버에서 root 로:
#   sudo MQTT_DOMAIN=daijin.inanna.day bash ~/esp32-iot/voice/ota/install-server.sh
# 두 번 돌려도 안전하다. 전제: voice/mosquitto/install.sh 가 먼저 돌아 /etc/letsencrypt/live/<도메인> 이 있다.
#
# 구조:  https://<도메인>/fw/<토큰>/<이름>.bin   ← /var/www/daijin/fw/<토큰>/ (daijin 사용자가 scp 로 올린다)
#   - 토큰은 긴 난수 디렉토리 이름. 바이너리에는 WiFi·브로커 비밀번호가 들어 있으니 주소를 모르면 못 받게 한다 (autoindex 없음).
#     토큰은 /var/www/daijin/fw/.token 과 맥 secrets 의 FW_TOKEN 에 같은 값. (sketches/ota.sh 가 쓴다)
#   - 80 은 certbot 의 ACME 확인(webroot)만 받고 나머지는 443 으로 보낸다. mosquitto/install.sh 의 standalone 방식은
#     nginx 가 80 을 차지하면 못 쓰므로 갱신을 webroot 로 바꾼다. mosquitto 쪽 deploy hook 은 그대로 돈다.
set -e
[ "$(id -u)" = 0 ] || { echo "root 로 (sudo) 돌릴 것"; exit 1; }
: "${MQTT_DOMAIN:?MQTT_DOMAIN=daijin.inanna.day 이 필요하다}"
LIVE=/etc/letsencrypt/live/$MQTT_DOMAIN
[ -d "$LIVE" ] || { echo "인증서가 없다: 먼저 voice/mosquitto/install.sh"; exit 1; }

apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx >/dev/null

mkdir -p /var/www/certbot /var/www/daijin/fw
if [ ! -s /var/www/daijin/fw/.token ]; then
  tr -dc 'a-z0-9' </dev/urandom | head -c 32 > /var/www/daijin/fw/.token
fi
TOKEN="$(cat /var/www/daijin/fw/.token)"
mkdir -p "/var/www/daijin/fw/$TOKEN"
chown -R daijin:daijin /var/www/daijin; chmod 750 /var/www/daijin /var/www/daijin/fw; chmod 600 /var/www/daijin/fw/.token
chmod 755 "/var/www/daijin/fw/$TOKEN"; chmod 755 /var/www/daijin /var/www/daijin/fw   # nginx(www-data) 가 지나가야 한다. 토큰 디렉토리 이름이 비밀이다

cat > /etc/nginx/sites-available/daijin <<EOF
# daijin: 펌웨어 내려받기 (voice/ota/install-server.sh 가 만든다)
server {
    listen 80;
    server_name $MQTT_DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://\$host\$request_uri; }
}
server {
    listen 443 ssl http2;        # Ubuntu 24.04 의 nginx 1.24: 'http2 on' 은 1.25 부터
    server_name $MQTT_DOMAIN;
    ssl_certificate     $LIVE/fullchain.pem;
    ssl_certificate_key $LIVE/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    location /fw/ {
        root /var/www/daijin;
        autoindex off;
        types { application/octet-stream bin; }
        default_type application/octet-stream;
    }
    location = /fw/.token { return 404; }
    location / { return 404; }
}
EOF
ln -sf /etc/nginx/sites-available/daijin /etc/nginx/sites-enabled/daijin
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx
systemctl reload nginx

# 갱신을 webroot 로: nginx 가 80 을 쓰므로 standalone 은 더 못 쓴다
certbot certonly --webroot -w /var/www/certbot -d "$MQTT_DOMAIN" --non-interactive --keep-until-expiring >/dev/null
HOOK=/etc/letsencrypt/renewal-hooks/deploy/nginx-reload.sh
printf '#!/bin/bash\nsystemctl reload nginx\n' > "$HOOK"; chmod +x "$HOOK"
certbot renew --dry-run >/dev/null && echo "인증서 갱신(webroot) 확인"

command -v ufw >/dev/null && ufw status | grep -q "^Status: active" && ufw allow 443/tcp >/dev/null
echo "펌웨어 자리 준비: https://$MQTT_DOMAIN/fw/<토큰>/<이름>.bin  (디렉토리 /var/www/daijin/fw/$TOKEN)"
echo "맥 secrets.local.txt 에 넣을 것:  FW_TOKEN=$TOKEN"
