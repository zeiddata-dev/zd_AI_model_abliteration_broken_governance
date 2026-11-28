#!/usr/bin/env bash
# setup_li_accounts.sh
# Installs the two small services behind Li's sign-in and voice. Both listen on 127.0.0.1 only.
#   li-tts   Kokoro 82M (open weights) speaking on the box CPU, port 3012. No outside service.
#   li-auth  invites, Face ID passkeys and the voice relay, port 3010. Runs inside the open-webui
#            container so it uses Open WebUI's own account code. Stores only hashed invite codes and
#            passkey public keys, in the open-webui data volume (li/li_auth.db).
# Then run: GATE=off bash setup_li_ui.sh <ref>   (adds the /li-auth/* route to Caddy)
# Usage: bash setup_li_accounts.sh [git ref]
REF=${1:-claude/qwen27b-openwebui}
RAW=https://raw.githubusercontent.com/zeiddata-dev/zd_ai_model_abliteration_broken_governance/$REF
DOMAIN=chat.zeiddata.com
TS=$(date -u +%Y%m%dT%H%M%SZ); LOG=/tmp/li_accounts_${TS}.log
exec > >(tee -a "$LOG") 2>&1
ok(){ echo "PASS  $1"; }; bad(){ echo "FAIL  $1"; echo "DECISION=$2"; exit 1; }
W=$(mktemp -d); cd "$W" || exit 1
for f in li/svc/li_auth.py li/svc/li_auth_stop.py li/svc/li_tts.py; do curl -fsSL --max-time 30 "$RAW/$f" -o "$(basename $f)" || bad "download $f" ABORT_DOWNLOAD; done

# 1. voice
sudo mkdir -p /opt/li-tts
[ -x /opt/li-tts/venv/bin/python ] || { sudo python3 -m venv /opt/li-tts/venv && sudo /opt/li-tts/venv/bin/pip -q install kokoro-onnx==0.6.1 soundfile fastapi uvicorn; } || bad "voice packages" ABORT_TTS_PIP
for f in kokoro-v1.0.onnx voices-v1.0.bin; do [ -s /opt/li-tts/$f ] || sudo curl -fsSL -o /opt/li-tts/$f https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/$f; done
( cd /opt/li-tts && printf '%s\n' "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5  kokoro-v1.0.onnx" "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d  voices-v1.0.bin" | sha256sum -c --quiet ) || bad "voice model checksum" ABORT_TTS_HASH
sudo install -m 644 li_tts.py /opt/li-tts/li_tts.py
sudo tee /etc/systemd/system/li-tts.service >/dev/null <<U
[Unit]
Description=Li voice (Kokoro, local only)
After=network.target
[Service]
Environment=LI_TTS_HOME=/opt/li-tts LI_TTS_PORT=3012 HOME=/tmp OMP_NUM_THREADS=3
ExecStart=/opt/li-tts/venv/bin/python /opt/li-tts/li_tts.py
DynamicUser=yes
Restart=always
MemoryMax=2G
Nice=5
[Install]
WantedBy=multi-user.target
U
sudo systemctl daemon-reload; sudo systemctl enable -q li-tts; sudo systemctl restart li-tts
for i in $(seq 60); do curl -sf 127.0.0.1:3012/health >/dev/null && break; sleep 2; done
curl -sf 127.0.0.1:3012/health >/dev/null && ok "voice on 127.0.0.1:3012" || bad "voice did not start" ABORT_TTS

# 2. li-auth inside the open-webui container
sudo docker exec open-webui sh -c 'mkdir -p /app/backend/data/li && pip -q install --no-deps --target /app/backend/data/li/pylib webauthn==3.0.1 cbor2 pyasn1 pyasn1-modules pyOpenSSL' 2>&1 | grep -v -i notice
sudo docker cp li_auth.py open-webui:/app/backend/data/li/li_auth.py || bad "copy li_auth" ABORT_COPY
sudo docker cp li_auth_stop.py open-webui:/app/backend/data/li/li_auth_stop.py || bad "copy li_auth_stop" ABORT_COPY
sudo tee /etc/systemd/system/li-auth.service >/dev/null <<U
[Unit]
Description=Li sign-in helper (invites, passkeys, voice relay), runs inside open-webui
After=docker.service
Requires=docker.service
[Service]
ExecStartPre=-/usr/bin/docker exec open-webui python /app/backend/data/li/li_auth_stop.py
ExecStart=/usr/bin/docker exec -e LI_PORT=3010 -e LI_RP_ID=$DOMAIN -e LI_ORIGIN=https://$DOMAIN -e LI_OWUI=http://127.0.0.1:3000 -e LI_TTS=http://127.0.0.1:3012 open-webui sh -c 'echo \$\$\$\$ > /tmp/li_auth.pid; exec python /app/backend/data/li/li_auth.py'
ExecStop=-/usr/bin/docker exec open-webui python /app/backend/data/li/li_auth_stop.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
U
sudo systemctl daemon-reload; sudo systemctl enable -q li-auth; sudo systemctl restart li-auth
for i in $(seq 60); do curl -sf 127.0.0.1:3010/li-auth/health >/dev/null && break; sleep 2; done
H=$(curl -s 127.0.0.1:3010/li-auth/health); echo "li_auth_health=$H"
echo "$H" | grep -q "\"rp_id\":\"$DOMAIN\"" && ok "li-auth on 127.0.0.1:3010" || { sudo journalctl -u li-auth -n 20 --no-pager; bad "li-auth did not start" ABORT_AUTH; }
echo "ROLLBACK=sudo systemctl disable --now li-auth li-tts"
echo "DECISION=READY"
