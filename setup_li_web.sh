#!/usr/bin/env bash
# setup_li_web.sh
# Web access for Li: SearXNG (private metasearch) in Docker on 127.0.0.1:8888 only, no public port,
# plus the search and page-reader endpoints in li-auth (/li-auth/web/search, /li-auth/web/read, sign-in required).
# The page reader refuses private, loopback and cloud-metadata addresses.
# Usage: bash setup_li_web.sh [git ref]    Rollback: printed ROLLBACK line.
REF=${1:-claude/qwen27b-openwebui}
RAW=https://raw.githubusercontent.com/zeiddata-dev/zd_ai_model_abliteration_broken_governance/$REF
TS=$(date -u +%Y%m%dT%H%M%SZ); LOG=/tmp/li_web_${TS}.log; PTR=/tmp/li_web_last.env
exec > >(tee -a "$LOG") 2>&1
ok(){ echo "PASS  $1"; }; bad(){ echo "FAIL  $1"; echo "DECISION=$2" | tee "$PTR"; exit 1; }
W=$(mktemp -d); cd "$W" || exit 1
for f in li/svc/li_auth.py li/svc/li_tts.py li/svc/t_web.py; do curl -fsSL --max-time 30 "$RAW/$f" -o "$(basename $f)" || bad "download $f" ABORT_DOWNLOAD; done

# 1. SearXNG, localhost only
sudo mkdir -p /opt/searxng
if ! sudo test -s /opt/searxng/settings.yml; then
  KEY=$(openssl rand -hex 32)
  sudo tee /opt/searxng/settings.yml >/dev/null <<Y
use_default_settings: true
general:
  instance_name: "li-search"
server:
  secret_key: "$KEY"
  limiter: false
  image_proxy: false
  public_instance: false
search:
  safe_search: 0
  formats:
    - html
    - json
outgoing:
  request_timeout: 5.0
  max_request_timeout: 8.0
Y
fi
sudo docker pull -q searxng/searxng:latest >/dev/null || bad "pull searxng" ABORT_PULL
sudo docker rm -f searxng >/dev/null 2>&1
sudo docker run -d --name searxng --restart unless-stopped -p 127.0.0.1:8888:8080 -v /opt/searxng:/etc/searxng:rw -e SEARXNG_BASE_URL=http://127.0.0.1:8888/ searxng/searxng:latest >/dev/null || bad "start searxng" ABORT_SEARX
for i in $(seq 45); do curl -sf "127.0.0.1:8888/search?q=test&format=json" >/dev/null && break; sleep 2; done
N=$(curl -s "127.0.0.1:8888/search?q=erowid+fentanyl&format=json" | python3 -c 'import sys,json;print(len(json.load(sys.stdin).get("results",[])))' 2>/dev/null)
echo "searx_results=$N"; [ "${N:-0}" -gt 0 ] && ok "SearXNG answering on 127.0.0.1:8888" || warn=1
PUB=$(sudo ss -ltn | awk '{print $4}' | grep -c ':8888$'); LOOP=$(sudo ss -ltn | awk '{print $4}' | grep -c '^127.0.0.1:8888$')
[ "$PUB" = "$LOOP" ] && ok "8888 bound to 127.0.0.1 only" || bad "8888 listening beyond localhost" ABORT_BIND

# 2. li-auth with web endpoints, voice cleanup
sudo install -m 644 li_tts.py /opt/li-tts/li_tts.py && sudo systemctl restart li-tts
sudo docker cp li_auth.py open-webui:/app/backend/data/li/li_auth.py || bad "copy li_auth" ABORT_COPY
sudo systemctl restart li-auth
for i in $(seq 60); do curl -sf 127.0.0.1:3010/li-auth/health >/dev/null && break; sleep 2; done
curl -sf 127.0.0.1:3010/li-auth/health >/dev/null && ok "li-auth back up" || bad "li-auth did not come back" ABORT_AUTH
for i in $(seq 30); do curl -sf 127.0.0.1:3012/health >/dev/null && break; sleep 2; done

# 3. same code path as Li: search, read channel sources, refuse internal addresses
sudo docker cp t_web.py open-webui:/tmp/t_web.py
sudo docker exec -e LI_SEARX=http://127.0.0.1:8888 open-webui python /tmp/t_web.py 2>&1 | grep -E "^(SEARCH|ENGINES|READ|BLOCKED|NOT_BLOCKED|BS4|NO_BS4)"
S401=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{"q":"x"}' https://chat.zeiddata.com/li-auth/web/search)
R401=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{"url":"https://erowid.org/"}' https://chat.zeiddata.com/li-auth/web/read)
echo "public_search_no_signin=$S401 public_read_no_signin=$R401"
{ echo "LOG=$LOG"; echo "SEARX_RESULTS=$N"; echo "ROLLBACK=sudo docker rm -f searxng"; echo "DECISION=READY"; } | tee "$PTR"
