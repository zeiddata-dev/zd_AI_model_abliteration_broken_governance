#!/usr/bin/env bash
# setup_li_ui.sh
# Puts the Li chat front end at https://chat.zeiddata.com/ on the box from setup_chat_web.sh.
# Same two locks as before: the private setup link cookie (Caddy) and the Open WebUI account login.
#   /            -> Li (static page in /var/www/li)
#   /li/*        -> Li assets (three.js, fonts, starter notes), all served from this box
#   everything else (/api, /admin, /auth, ...) -> Open WebUI on 127.0.0.1:3000, unchanged
# Li talks to the model through Open WebUI's /api/chat/completions with the signed-in user's token.
# Rollback: the printed ROLLBACK line restores the previous Caddyfile.
# Usage: bash setup_li_ui.sh [git ref]   (default: claude/qwen27b-openwebui)
#        GATE=off bash setup_li_ui.sh [git ref]   no private link, login only, sign-ups closed

REF=${1:-claude/qwen27b-openwebui}
GATE=${GATE:-on}   # GATE=off drops the private-link gate (login only, sign-ups closed)
TS=$(date -u +%Y%m%dT%H%M%SZ)
LOG=/tmp/li_ui_setup_${TS}.log
PTR=/tmp/li_ui_last_setup.env
WORK=/opt/qwen27b
WWW=/var/www/li
BACKUP=/tmp/li_ui_backup_${TS}
DOMAIN=chat.zeiddata.com
WEBUI_PORT=3000
RAW=https://raw.githubusercontent.com/zeiddata-dev/zd_ai_model_abliteration_broken_governance/$REF/li/index.html
GATE_FILE=$WORK/gate_token
GRN=$'\033[0;32m'; RED=$'\033[0;31m'; YEL=$'\033[0;33m'; NC=$'\033[0m'
exec > >(tee -a "$LOG") 2>&1
pass(){ printf "%sPASS%s  %s\n" "$GRN" "$NC" "$1"; }
warn(){ printf "%sWARN%s  %s\n" "$YEL" "$NC" "$1"; }
fail(){ printf "%sFAIL%s  %s\n" "$RED" "$NC" "$1"; }
PAGE=NOT_RUN; VENDOR=NOT_RUN; FONTS=NOT_RUN; CADDY=NOT_RUN; ROUTES=NOT_RUN; DECISION=UNSET

finish(){
  DECISION=$1
  {
    echo "LOG=$LOG"; echo "WWW=$WWW"; echo "BACKUP=$BACKUP"; echo "REF=$REF"
    echo "GATE=$GATE"; echo "PAGE=$PAGE"; echo "VENDOR=$VENDOR"; echo "FONTS=$FONTS"; echo "CADDY=$CADDY"; echo "ROUTES=$ROUTES"
    echo "ROLLBACK=sudo cp -a $BACKUP/Caddyfile.bak /etc/caddy/Caddyfile && sudo systemctl reload caddy"
    echo "DECISION=$DECISION"; echo "SETUP_UTC=$(date -u +%FT%TZ)"
  } > "$PTR"
  echo ""; echo "===== [SEND ONLY THIS] ====="; cat "$PTR"; echo "===== [END SEND ONLY THIS] ====="
  echo "exit_code=$2"; exit "$2"
}

echo "==== Li UI setup start $(date -u +%FT%TZ) ===="
cd /tmp || exit 1
sudo mkdir -p "$BACKUP" "$WWW/li/vendor/fonts"
if ! sudo test -s "$GATE_FILE"; then fail "No gate token at $GATE_FILE. Run setup_chat_web.sh first."; finish ABORT_NO_GATE 1; fi
GATE_TOKEN=$(sudo cat "$GATE_FILE")

# 1. The page
TMP=$(mktemp)
if curl -fsSL --max-time 30 "$RAW" -o "$TMP" && grep -q '<title>Li</title>' "$TMP"; then
  [ -f "$WWW/index.html" ] && sudo cp -a "$WWW/index.html" "$BACKUP/index.html.bak"
  sudo install -m 644 "$TMP" "$WWW/index.html"
  PAGE=OK; pass "Li page installed ($(wc -c < "$TMP") bytes, sha256 $(sha256sum "$TMP" | cut -c1-12))"
else
  PAGE=FAILED; fail "Could not download the Li page from $RAW"; finish ABORT_PAGE 1
fi
rm -f "$TMP"

# 2. three.js for the 3D Z, served from this box (no CDN calls from the browser)
V=$WWW/li/vendor
sudo curl -fsSL --max-time 30 https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js -o "$V/three.min.js"
sudo curl -fsSL --max-time 30 https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/environments/RoomEnvironment.js -o "$V/RoomEnvironment.js"
if [ "$(stat -c %s "$V/three.min.js" 2>/dev/null || echo 0)" -gt 500000 ] && grep -q RoomEnvironment "$V/RoomEnvironment.js" 2>/dev/null; then
  VENDOR=OK; pass "three.js r128 + RoomEnvironment saved locally"
else
  VENDOR=FAILED; warn "three.js download failed. The logo falls back to the flat Z."
fi

# 3. Fonts, also served from this box
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36'
CSS_URL='https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&family=JetBrains+Mono:wght@400&display=swap'
CSS=$(curl -fsSL --max-time 30 -A "$UA" "$CSS_URL")
if [ -n "$CSS" ]; then
  N=0
  for u in $(printf '%s' "$CSS" | grep -o 'https://fonts.gstatic.com/[^)]*\.woff2' | sort -u); do
    f=$(printf '%s' "$u" | sha1sum | cut -c1-16).woff2
    sudo curl -fsSL --max-time 30 "$u" -o "$V/fonts/$f" && N=$((N+1))
    CSS=${CSS//"$u"//li/vendor/fonts/$f}
  done
  printf '%s\n' "$CSS" | sudo tee "$V/fonts.css" >/dev/null
  FONTS="OK_$N"; pass "Fonts saved locally ($N files)"
else
  echo "/* fonts unavailable, system fonts in use */" | sudo tee "$V/fonts.css" >/dev/null
  FONTS=FALLBACK; warn "Font download failed, using system fonts"
fi

# 4. Starter notes (read-only memory until Lithium is connected). Kept if already there.
if [ ! -f "$WWW/li/notes.json" ]; then
  echo '{"notes":[]}' | sudo tee "$WWW/li/notes.json" >/dev/null
  warn "No starter notes yet ($WWW/li/notes.json is empty)"
fi
sudo chown -R root:root "$WWW"; sudo find "$WWW" -type d -exec chmod 755 {} +; sudo find "$WWW" -type f -exec chmod 644 {} +

# 5. Caddy: same gate, Li at / and /li/*, Open WebUI for everything else
sudo cp -a /etc/caddy/Caddyfile "$BACKUP/Caddyfile.bak"
if [ "$GATE" = "off" ]; then
# No private-link gate: anyone reaching the domain gets Li's sign-in. Open sign-ups are refused here;
# new accounts come only through an admin's invite (li-auth on 127.0.0.1:3010, see setup_li_accounts.sh).
sudo tee /etc/caddy/Caddyfile >/dev/null <<CADDYEOF
$DOMAIN {
	encode zstd gzip
	redir /setup/* / 302
	redir /li / 302
	@signup path /api/v1/auths/signup
	handle @signup {
		respond "Sign-ups are closed" 403
	}
	handle /li-auth/* {
		reverse_proxy 127.0.0.1:3010
	}
	@li path / /index.html /li/*
	handle @li {
		root * $WWW
		header /index.html Cache-Control "no-cache"
		header / Cache-Control "no-cache"
		header /li/notes.json Cache-Control "no-store"
		file_server
	}
	handle {
		reverse_proxy 127.0.0.1:$WEBUI_PORT
	}
}
CADDYEOF
else
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
		@li path / /index.html /li/*
		handle @li {
			root * $WWW
			header /index.html Cache-Control "no-cache"
			header / Cache-Control "no-cache"
			header /li/notes.json Cache-Control "no-store"
			file_server
		}
		handle {
			reverse_proxy 127.0.0.1:$WEBUI_PORT
		}
	}
	handle {
		respond "Not available" 403
	}
}
CADDYEOF
fi
sudo chmod 640 /etc/caddy/Caddyfile
sudo chown root:caddy /etc/caddy/Caddyfile
if sudo caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
  pass "Caddyfile valid"
else
  fail "Caddyfile invalid, putting the old one back"; sudo caddy validate --config /etc/caddy/Caddyfile 2>&1 | tail -5
  sudo cp -a "$BACKUP/Caddyfile.bak" /etc/caddy/Caddyfile
  CADDY=BAD_CONFIG; finish ABORT_CADDYFILE 1
fi
sudo systemctl reload caddy
sleep 2
if systemctl is-active --quiet caddy; then CADDY=OK; pass "Caddy reloaded"; else CADDY=FAILED; fail "Caddy not running"; finish ABORT_CADDY_SVC 1; fi

# 6. Route checks through the real HTTPS path
JAR=$(mktemp)
c(){ curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$@"; }
STRANGER=$(c "https://$DOMAIN/")
[ "$GATE" = "off" ] || c -c "$JAR" "https://$DOMAIN/setup/$GATE_TOKEN" >/dev/null
ROOT=$(curl -s --max-time 15 -b "$JAR" "https://$DOMAIN/" | grep -c '<title>Li</title>')
THREE=$(c -b "$JAR" "https://$DOMAIN/li/vendor/three.min.js")
NOTES=$(c -b "$JAR" "https://$DOMAIN/li/notes.json")
CFG=$(curl -s --max-time 15 -b "$JAR" "https://$DOMAIN/api/config" | grep -c '"version"')
ADMIN=$(c -b "$JAR" "https://$DOMAIN/admin/users")
SIGNUP=$(c -X POST -H 'Content-Type: application/json' -d '{}' "https://$DOMAIN/api/v1/auths/signup")
rm -f "$JAR"
LIAUTH=$(c "https://$DOMAIN/li-auth/health")
echo "li_auth=$LIAUTH gate=$GATE stranger=$STRANGER li_root=$ROOT three=$THREE notes=$NOTES api_config=$CFG admin=$ADMIN signup=$SIGNUP"
if [ "$GATE" = "off" ]; then WANT_STRANGER=200; else WANT_STRANGER=403; fi
if [ "$GATE" = "off" ] && [ "$SIGNUP" != "403" ]; then ROUTES="SIGNUP_OPEN_$SIGNUP"; warn "Sign-up is not blocked"; finish READY_CHECK 0; fi
if [ "$STRANGER" = "$WANT_STRANGER" ] && [ "$ROOT" = "1" ] && [ "$NOTES" = "200" ] && [ "$CFG" = "1" ] && [ "$ADMIN" = "200" ]; then
  ROUTES=OK; pass "Gate $GATE: front page $STRANGER, Li at /, Open WebUI API and admin reachable"
  finish READY 0
else
  ROUTES="CHECK_${STRANGER}_${ROOT}_${THREE}_${NOTES}_${CFG}_${ADMIN}"; warn "A route check did not match"
  finish READY_CHECK 0
fi
