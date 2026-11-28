#!/usr/bin/env bash
# setup_chat_web.sh
# Puts the Open WebUI chat (from setup_qwen27b_chat.sh) on a public HTTPS subdomain.
# Two locks:
#   1. Caddy only lets a browser through after it opens the private setup link once
#      (sets a long-lived cookie). Everyone else gets a 403.
#   2. Open WebUI's own login. The first account created becomes the admin.
# Needs: DNS A record for DOMAIN pointing at this box, and ports 80 + 443 open.

TS=$(date -u +%Y%m%dT%H%M%SZ)
LOG=/tmp/chat_web_setup_${TS}.log
PTR=/tmp/chat_web_last_setup.env
WORK=/opt/qwen27b
BACKUP=/tmp/chat_web_backup_${TS}
DOMAIN=chat.zeiddata.com
MODEL=huihui_ai/Qwen3.8-abliterated:27b-q4_K_M
WEBUI_PORT=3000
WEBUI_IMAGE=ghcr.io/open-webui/open-webui:main
WEBUI_NAME=open-webui
WEBUI_VOLUME=open-webui-auth
GATE_FILE=$WORK/gate_token
SECRET_FILE=$WORK/webui_secret_key
GRN=$'\033[0;32m'; RED=$'\033[0;31m'; YEL=$'\033[0;33m'; NC=$'\033[0m'
exec > >(tee -a "$LOG") 2>&1
pass(){ printf "%sPASS%s  %s\n" "$GRN" "$NC" "$1"; }
warn(){ printf "%sWARN%s  %s\n" "$YEL" "$NC" "$1"; }
fail(){ printf "%sFAIL%s  %s\n" "$RED" "$NC" "$1"; }
CADDY=NOT_RUN; WEBUI=NOT_RUN; CERT=NOT_RUN; GATE=NOT_RUN; DECISION=UNSET

finish(){
  DECISION=$1
  {
    echo "LOG=$LOG"; echo "WORK=$WORK"; echo "BACKUP=$BACKUP"; echo "DOMAIN=$DOMAIN"
    echo "CADDY=$CADDY"; echo "WEBUI=$WEBUI"; echo "CERT=$CERT"; echo "GATE=$GATE"
    echo "DECISION=$DECISION"; echo "SETUP_UTC=$(date -u +%FT%TZ)"
  } > "$PTR"
  echo ""; echo "===== [SEND ONLY THIS] ====="; cat "$PTR"; echo "===== [END SEND ONLY THIS] ====="
  echo "exit_code=$2"; exit "$2"
}

echo "==== chat web setup start $(date -u +%FT%TZ) ===="
cd /tmp || exit 1
sudo mkdir -p "$WORK" "$BACKUP"
sudo chmod 700 "$WORK"

# 1. Secrets that must survive container restarts (never printed)
if ! sudo test -s "$GATE_FILE"; then
  openssl rand -hex 24 | sudo tee "$GATE_FILE" >/dev/null
fi
if ! sudo test -s "$SECRET_FILE"; then
  openssl rand -hex 32 | sudo tee "$SECRET_FILE" >/dev/null
fi
sudo chmod 600 "$GATE_FILE" "$SECRET_FILE"
GATE_TOKEN=$(sudo cat "$GATE_FILE")
WEBUI_SECRET=$(sudo cat "$SECRET_FILE")
pass "Gate token and app secret stored in $WORK (root only)"

# 2. Caddy (only installs if missing)
if ! command -v caddy >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
  curl -1sLf "https://dl.cloudsmith.io/public/caddy/stable/gpg.key" | sudo gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf "https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt" | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y caddy
fi
if command -v caddy >/dev/null 2>&1; then
  pass "Caddy installed: $(caddy version | awk '{print $1}')"
else
  fail "Caddy install failed."
  CADDY=INSTALL_FAILED; finish ABORT_CADDY 1
fi

# 3. Open WebUI with its own login turned on (fresh volume; the old no-login volume is kept)
if sudo docker ps -a --format '{{.Names}}' | grep -qx "$WEBUI_NAME"; then
  sudo docker inspect "$WEBUI_NAME" > "$BACKUP/open-webui.inspect.json" 2>/dev/null
  sudo docker rm -f "$WEBUI_NAME" >/dev/null
  pass "Old chat container settings saved to $BACKUP; old volume open-webui kept"
fi
sudo docker run -d \
  --name "$WEBUI_NAME" \
  --restart unless-stopped \
  --network host \
  -v "$WEBUI_VOLUME":/app/backend/data \
  -e HOST=127.0.0.1 \
  -e PORT="$WEBUI_PORT" \
  -e OLLAMA_BASE_URL=http://127.0.0.1:11434 \
  -e WEBUI_AUTH=True \
  -e WEBUI_SECRET_KEY="$WEBUI_SECRET" \
  -e WEBUI_URL="https://$DOMAIN" \
  -e CORS_ALLOW_ORIGIN="https://$DOMAIN" \
  -e DEFAULT_USER_ROLE=pending \
  -e ENABLE_OPENAI_API=False \
  -e DEFAULT_MODELS="$MODEL" \
  -e ANONYMIZED_TELEMETRY=False \
  -e DO_NOT_TRACK=True \
  -e SCARF_NO_ANALYTICS=True \
  "$WEBUI_IMAGE" >/dev/null
for i in $(seq 1 120); do
  curl -sf "http://127.0.0.1:${WEBUI_PORT}/health" >/dev/null 2>&1 && break
  sleep 5
done
if curl -sf "http://127.0.0.1:${WEBUI_PORT}/health" >/dev/null 2>&1; then
  WEBUI=OK; pass "Open WebUI up on 127.0.0.1:${WEBUI_PORT} with login on"
else
  WEBUI=FAILED; fail "Open WebUI did not come up"; sudo docker logs --tail 40 "$WEBUI_NAME"
  finish ABORT_WEBUI 1
fi

# 4. Caddy config: HTTPS, private setup link sets a cookie, everything else 403
if [ -f /etc/caddy/Caddyfile ]; then
  sudo cp -a /etc/caddy/Caddyfile "$BACKUP/Caddyfile.bak"
fi
sudo tee /etc/caddy/Caddyfile >/dev/null <<CADDYEOF
$DOMAIN {
	encode zstd gzip
	@setup path /setup/$GATE_TOKEN
	handle @setup {
		header Set-Cookie "zd_gate=$GATE_TOKEN; Path=/; Max-Age=31536000; Secure; HttpOnly; SameSite=Lax"
		redir * / 302
	}
	@gated header Cookie *zd_gate=$GATE_TOKEN*
	handle @gated {
		reverse_proxy 127.0.0.1:$WEBUI_PORT
	}
	handle {
		respond "Not available" 403
	}
}
CADDYEOF
sudo chmod 640 /etc/caddy/Caddyfile
sudo chown root:caddy /etc/caddy/Caddyfile
if sudo caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
  pass "Caddyfile valid"
else
  fail "Caddyfile invalid"; sudo caddy validate --config /etc/caddy/Caddyfile
  CADDY=BAD_CONFIG; finish ABORT_CADDYFILE 1
fi
sudo systemctl enable caddy >/dev/null 2>&1
sudo systemctl restart caddy
sleep 3
if systemctl is-active --quiet caddy; then
  CADDY=OK; pass "Caddy running"
else
  CADDY=FAILED; fail "Caddy not running"; sudo journalctl -u caddy --no-pager -n 30
  finish ABORT_CADDY_SVC 1
fi

# 5. Wait for the certificate, then test both locks from the outside path
for i in $(seq 1 36); do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://$DOMAIN/")
  [ "$CODE" = "403" ] && break
  sleep 5
done
if [ "$CODE" = "403" ]; then
  CERT=OK; pass "HTTPS works and strangers get 403"
else
  CERT="CHECK_$CODE"; warn "HTTPS not answering yet (got $CODE). Caddy keeps retrying the certificate."
  sudo journalctl -u caddy --no-pager -n 15 | cut -c1-200
fi
JAR=$(mktemp)
SCODE=$(curl -s -o /dev/null -w '%{http_code}' -c "$JAR" --max-time 10 "https://$DOMAIN/setup/$GATE_TOKEN")
PCODE=$(curl -s -o /dev/null -w '%{http_code}' -b "$JAR" --max-time 15 "https://$DOMAIN/")
rm -f "$JAR"
if [ "$SCODE" = "302" ] && [ "$PCODE" = "200" ]; then
  GATE=OK; pass "Setup link lets a browser through to the login page"
else
  GATE="CHECK_${SCODE}_${PCODE}"; warn "Setup link check: setup=$SCODE page=$PCODE"
fi

if [ "$CERT" = "OK" ] && [ "$GATE" = "OK" ]; then
  finish READY 0
else
  finish READY_CHECK 0
fi
