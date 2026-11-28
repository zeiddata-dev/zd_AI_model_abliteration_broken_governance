#!/usr/bin/env bash
# setup_keep_warm.sh
# Keeps the 27B loaded on the GPU all the time, so the chat never waits 2-3 minutes for a cold load.
#  - OLLAMA_KEEP_ALIVE=-1 (never unload after idle)
#  - li-preload.service loads the model right after Ollama starts (so a reboot warms it too)
# The box is a dedicated GPU instance, so holding the model in VRAM costs nothing extra.

TS=$(date -u +%Y%m%dT%H%M%SZ)
LOG=/tmp/keep_warm_${TS}.log
PTR=/tmp/keep_warm_last.env
BACKUP=/tmp/keep_warm_backup_${TS}
MODEL=huihui_ai/Qwen3.8-abliterated:27b-q4_K_M
OVR=/etc/systemd/system/ollama.service.d/override.conf
GRN=$'\033[0;32m'; RED=$'\033[0;31m'; YEL=$'\033[0;33m'; NC=$'\033[0m'
exec > >(tee -a "$LOG") 2>&1
pass(){ printf "%sPASS%s  %s\n" "$GRN" "$NC" "$1"; }
warn(){ printf "%sWARN%s  %s\n" "$YEL" "$NC" "$1"; }
fail(){ printf "%sFAIL%s  %s\n" "$RED" "$NC" "$1"; }
KEEP=NOT_RUN; PRELOAD=NOT_RUN; WARM=NOT_RUN

finish(){
  { echo "LOG=$LOG"; echo "BACKUP=$BACKUP"; echo "KEEP=$KEEP"; echo "PRELOAD=$PRELOAD"; echo "WARM=$WARM"; echo "DECISION=$1"; echo "SETUP_UTC=$(date -u +%FT%TZ)"; } > "$PTR"
  echo ""; echo "===== [SEND ONLY THIS] ====="; cat "$PTR"; echo "===== [END SEND ONLY THIS] ====="
  echo "exit_code=$2"; exit "$2"
}

cd /tmp || exit 1
sudo mkdir -p "$BACKUP"
[ -f "$OVR" ] && sudo cp -a "$OVR" "$BACKUP/override.conf.bak"
if [ -f "$OVR" ] && sudo grep -q OLLAMA_KEEP_ALIVE "$OVR"; then
  sudo sed -i 's/^Environment="OLLAMA_KEEP_ALIVE=.*"/Environment="OLLAMA_KEEP_ALIVE=-1"/' "$OVR"
else
  sudo mkdir -p "$(dirname "$OVR")"
  printf '[Service]\nEnvironment="OLLAMA_KEEP_ALIVE=-1"\n' | sudo tee -a "$OVR" >/dev/null
fi
sudo grep -q 'OLLAMA_KEEP_ALIVE=-1' "$OVR" && { KEEP=OK; pass "Ollama keeps models loaded (KEEP_ALIVE=-1)"; } || { KEEP=FAILED; fail "Could not set KEEP_ALIVE"; finish ABORT_KEEP 1; }

sudo tee /usr/local/bin/li-preload.sh >/dev/null <<SCRIPT
#!/bin/sh
for i in \$(seq 1 60); do curl -sf http://127.0.0.1:11434/api/tags >/dev/null && break; sleep 2; done
curl -s --max-time 900 http://127.0.0.1:11434/api/generate -d '{"model":"$MODEL","keep_alive":-1}' >/dev/null
SCRIPT
sudo chmod 755 /usr/local/bin/li-preload.sh
sudo tee /etc/systemd/system/li-preload.service >/dev/null <<UNIT
[Unit]
Description=Load the 27B into the GPU right after Ollama starts
After=ollama.service
Requires=ollama.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/li-preload.sh
TimeoutStartSec=1000

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable li-preload.service >/dev/null 2>&1 && { PRELOAD=OK; pass "li-preload.service enabled (warms the model after every boot)"; } || { PRELOAD=FAILED; warn "Could not enable li-preload.service"; }

sudo systemctl restart ollama
sleep 3
echo "Loading the model now (can take a few minutes the first time)..."
sudo systemctl start li-preload.service
if curl -s http://127.0.0.1:11434/api/ps | grep -q "$MODEL"; then
  EXP=$(curl -s http://127.0.0.1:11434/api/ps | grep -o '"expires_at":"[^"]*"')
  WARM=OK; pass "Model is loaded ($EXP)"
  finish READY 0
else
  WARM=NOT_LOADED; warn "Model not loaded yet"; finish READY_CHECK 0
fi
