#!/usr/bin/env bash
LOG=/tmp/setup_proxy.log
PTR=/tmp/heretic_proxy.env
DOMAIN="CHANGE_ME.example.com"
MODEL=svjack/gpt-oss-20b-heretic
RED=$'\033[0;31m'
GRN=$'\033[0;32m'
NC=$'\033[0m'
exec > >(tee -a "$LOG") 2>&1
pass(){ printf "%sPASS%s  %s\n" "$GRN" "$NC" "$1"; }
fail(){ printf "%sFAIL%s  %s\n" "$RED" "$NC" "$1"; }
echo "==== proxy setup start $(date -u +%FT%TZ) ===="
if [ "$DOMAIN" = "CHANGE_ME.example.com" ]; then
  fail "Set DOMAIN at the top of this script to your real hostname first."
  echo "DECISION=ABORT_NO_DOMAIN"; echo "exit_code=1"; exit 1
fi
TOKEN=$(openssl rand -hex 32)
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/proxy.conf >/dev/null <<OLLAMA
[Service]
Environment="OLLAMA_HOST=127.0.0.1:11434"
Environment="OLLAMA_ORIGINS=*"
OLLAMA
sudo systemctl daemon-reload
sudo systemctl restart ollama
sleep 3
if systemctl is-active --quiet ollama; then
  pass "Ollama active on 127.0.0.1:11434"
else
  fail "Ollama not active."
  echo "DECISION=ABORT_OLLAMA"; echo "exit_code=1"; exit 1
fi
sudo apt-get update
sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
curl -1sLf "https://dl.cloudsmith.io/public/caddy/stable/gpg.key" | sudo gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf "https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt" | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
sudo apt-get update
sudo apt-get install -y caddy
if command -v caddy >/dev/null 2>&1; then
  pass "Caddy installed"
else
  fail "Caddy install failed."
  echo "DECISION=ABORT_CADDY"; echo "exit_code=1"; exit 1
fi
sudo tee /etc/caddy/Caddyfile >/dev/null <<CADDY
$DOMAIN {
    @authorized header Authorization "Bearer $TOKEN"
    handle @authorized {
        reverse_proxy localhost:11434 {
            header_up Host localhost:11434
        }
    }
    handle {
        respond "Unauthorized" 403
    }
}
CADDY
sudo systemctl restart caddy
sleep 3
if systemctl is-active --quiet caddy; then
  pass "Caddy active, proxying $DOMAIN to localhost:11434"
else
  fail "Caddy not active."
  echo "DECISION=ABORT_CADDY_SVC"; echo "exit_code=1"; exit 1
fi
{
  echo "DOMAIN=$DOMAIN"
  echo "TOKEN=$TOKEN"
  echo "ENDPOINT=https://$DOMAIN"
  echo "MODEL=$MODEL"
  echo "SETUP_UTC=$(date -u +%FT%TZ)"
} > "$PTR"
chmod 600 "$PTR"
pass "Pointer written: $PTR (contains token, keep private)"
echo ""
echo "SHARE WITH THE OTHER PARTY:"
echo "  Endpoint: https://$DOMAIN/api/generate"
echo "  Chat:     https://$DOMAIN/api/chat"
echo "  Token:    $TOKEN"
echo ""
echo "DECISION=READY"
echo "exit_code=0"
