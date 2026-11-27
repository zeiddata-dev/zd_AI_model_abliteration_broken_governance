#!/usr/bin/env bash
# setup_qwen27b_chat.sh
# One-shot install on a fresh g6.xlarge (1x NVIDIA L4, 24 GB):
#   Ollama + huihui_ai/Qwen3.8-abliterated:27b-q4_K_M + Open WebUI chat window.
# Everything listens on 127.0.0.1 only. You reach the chat page through an SSH tunnel.
# Safe to re-run: it skips what is already installed and backs up what it changes.

TS=$(date -u +%Y%m%dT%H%M%SZ)
LOG=/tmp/qwen27b_setup_${TS}.log
PTR=/tmp/qwen27b_last_setup.env
WORK=/opt/qwen27b
BACKUP=/tmp/qwen27b_backup_${TS}
MODEL=huihui_ai/Qwen3.8-abliterated:27b-q4_K_M
CTX=32768
WEBUI_PORT=3000
WEBUI_IMAGE=ghcr.io/open-webui/open-webui:main
WEBUI_NAME=open-webui
MIN_FREE_GB=35
RED=$'\033[0;31m'
GRN=$'\033[0;32m'
YEL=$'\033[0;33m'
NC=$'\033[0m'

exec > >(tee -a "$LOG") 2>&1
pass(){ printf "%sPASS%s  %s\n" "$GRN" "$NC" "$1"; }
warn(){ printf "%sWARN%s  %s\n" "$YEL" "$NC" "$1"; }
fail(){ printf "%sFAIL%s  %s\n" "$RED" "$NC" "$1"; }

GPU=NONE; DRIVER=MISSING; OLLAMA=MISSING; PULL=NOT_RUN; INFER=NOT_RUN
GPU_SPLIT=UNKNOWN; DOCKER=MISSING; WEBUI=NOT_RUN; BIND=UNKNOWN; DECISION=UNSET

write_ptr(){
  {
    echo "LOG=$LOG"
    echo "WORK=$WORK"
    echo "BACKUP=$BACKUP"
    echo "MODEL=$MODEL"
    echo "CTX=$CTX"
    echo "GPU=$GPU"
    echo "DRIVER=$DRIVER"
    echo "OLLAMA=$OLLAMA"
    echo "PULL=$PULL"
    echo "INFER=$INFER"
    echo "GPU_SPLIT=$GPU_SPLIT"
    echo "DOCKER=$DOCKER"
    echo "WEBUI=$WEBUI"
    echo "BIND=$BIND"
    echo "CHAT_URL=http://localhost:${WEBUI_PORT}"
    echo "DECISION=$DECISION"
    echo "SETUP_UTC=$(date -u +%FT%TZ)"
  } > "$PTR"
}

finish(){
  DECISION=$1
  write_ptr
  echo ""
  echo "===== [SEND ONLY THIS] ====="
  cat "$PTR"
  echo "===== [END SEND ONLY THIS] ====="
  echo "exit_code=$2"
  exit "$2"
}

echo "==== qwen27b chat setup start $(date -u +%FT%TZ) ===="
cd /tmp || exit 1
sudo mkdir -p "$WORK" "$BACKUP"

# 1. Disk space
FREE_GB=$(df -BG --output=avail / | tail -n1 | tr -dc '0-9')
if [ "${FREE_GB:-0}" -ge "$MIN_FREE_GB" ]; then
  pass "Free disk on /: ${FREE_GB} GB"
else
  fail "Only ${FREE_GB} GB free on /. Need ${MIN_FREE_GB} GB (model 18 GB + chat app ~5 GB + room). Grow the EBS volume to 100 GB."
  finish ABORT_DISK 1
fi

# 2. NVIDIA driver (only installs if missing)
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
  DRIVER=PRESENT
  GPU=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -n1 | tr -d ',')
  pass "GPU detected: $GPU"
else
  warn "No working NVIDIA driver. Installing the recommended Ubuntu driver."
  sudo apt-get update
  sudo apt-get install -y ubuntu-drivers-common
  sudo ubuntu-drivers install
  DRIVER=INSTALLED_NEEDS_REBOOT
  echo "Driver installed. Reboot with: sudo reboot   then run this same script again."
  finish REBOOT_THEN_RERUN 0
fi
VRAM_MIB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -n1 | tr -dc '0-9')
if [ "${VRAM_MIB:-0}" -lt 22000 ]; then
  warn "GPU has ${VRAM_MIB} MiB. The 27B q4_K_M wants about 22 GB. Expect it to spill to CPU and crawl."
fi

# 3. Ollama (only installs if missing)
if ! command -v ollama >/dev/null 2>&1; then
  curl -fsSL https://ollama.com/install.sh | sh
fi
if command -v ollama >/dev/null 2>&1; then
  OLLAMA="$(ollama --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | tail -n1)"
  pass "Ollama installed: $OLLAMA"
else
  fail "Ollama install failed."
  finish ABORT_OLLAMA_INSTALL 1
fi

# 4. Ollama settings: localhost only, 32K context, flash attention, half-size KV cache
sudo mkdir -p /etc/systemd/system/ollama.service.d
if [ -f /etc/systemd/system/ollama.service.d/override.conf ]; then
  sudo cp -a /etc/systemd/system/ollama.service.d/override.conf "$BACKUP/override.conf.bak"
  pass "Backed up existing override.conf to $BACKUP"
fi
sudo tee /etc/systemd/system/ollama.service.d/override.conf >/dev/null <<OVR
[Service]
Environment="OLLAMA_HOST=127.0.0.1:11434"
Environment="OLLAMA_CONTEXT_LENGTH=$CTX"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
Environment="OLLAMA_KEEP_ALIVE=-1"
Environment="OLLAMA_NUM_PARALLEL=1"
OVR
sudo systemctl daemon-reload
sudo systemctl enable ollama >/dev/null 2>&1
sudo systemctl restart ollama
for i in $(seq 1 30); do
  curl -s http://127.0.0.1:11434/api/version >/dev/null 2>&1 && break
  sleep 2
done
if curl -s http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
  pass "Ollama answering on 127.0.0.1:11434 (context $CTX, flash attention on, KV cache q8_0)"
else
  fail "Ollama service not answering."
  sudo journalctl -u ollama --no-pager -n 30
  finish ABORT_SERVICE 1
fi

# 5. Pull the model (about 18 GB, the long part)
echo "Pulling $MODEL (about 18 GB). This is the slow step."
ollama pull "$MODEL"
if ollama list | awk '{print $1}' | grep -qx "$MODEL"; then
  PULL=OK
  pass "Model pulled: $MODEL"
else
  PULL=FAILED
  fail "Model pull failed: $MODEL"
  finish ABORT_PULL 1
fi

# 6. Smoke test (first load is cold, can take a few minutes)
echo "Loading the model onto the GPU and asking it one question."
RESP=$(curl -s --max-time 900 http://127.0.0.1:11434/api/chat -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with one word: online\"}],\"stream\":false,\"keep_alive\":\"30m\",\"options\":{\"num_predict\":2048}}")
if echo "$RESP" | grep -q '"content"\|"thinking"'; then
  INFER=OK
  pass "Inference responded"
  echo "$RESP" | head -c 400; echo ""
else
  INFER=CHECK
  fail "Empty or malformed inference response:"
  echo "$RESP" | head -c 400; echo ""
fi
GPU_SPLIT=$(ollama ps | awk 'NR>1 {for(i=1;i<=NF;i++) if($i ~ /GPU|CPU/){print $(i-1)"_"$i; exit}}')
[ -z "$GPU_SPLIT" ] && GPU_SPLIT=UNKNOWN
ollama ps
if echo "$GPU_SPLIT" | grep -q '100%_GPU'; then
  pass "Model fully on the GPU ($GPU_SPLIT)"
else
  warn "Model not fully on the GPU ($GPU_SPLIT). Lower CTX at the top of this script to 16384 and re-run."
fi
nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader

# 7. Docker (only installs if missing)
if ! command -v docker >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y docker.io
fi
sudo systemctl enable --now docker >/dev/null 2>&1
if sudo docker info >/dev/null 2>&1; then
  DOCKER="$(sudo docker --version)"
  pass "Docker running: $DOCKER"
else
  fail "Docker not running."
  finish ABORT_DOCKER 1
fi

# 8. Open WebUI chat window, localhost only, no login screen (single user, tunnel only)
if sudo docker ps -a --format '{{.Names}}' | grep -qx "$WEBUI_NAME"; then
  sudo docker inspect "$WEBUI_NAME" > "$BACKUP/open-webui.inspect.json" 2>/dev/null
  pass "Saved old container settings to $BACKUP (chat history volume is kept)"
  sudo docker rm -f "$WEBUI_NAME" >/dev/null
fi
sudo docker pull "$WEBUI_IMAGE"
sudo docker run -d \
  --name "$WEBUI_NAME" \
  --restart unless-stopped \
  --network host \
  -v open-webui:/app/backend/data \
  -e HOST=127.0.0.1 \
  -e PORT="$WEBUI_PORT" \
  -e OLLAMA_BASE_URL=http://127.0.0.1:11434 \
  -e WEBUI_AUTH=False \
  -e ENABLE_OPENAI_API=False \
  -e DEFAULT_MODELS="$MODEL" \
  -e ANONYMIZED_TELEMETRY=False \
  -e DO_NOT_TRACK=True \
  -e SCARF_NO_ANALYTICS=True \
  "$WEBUI_IMAGE" >/dev/null
echo "Waiting for the chat window to come up (first start can take a few minutes)."
for i in $(seq 1 120); do
  curl -sf "http://127.0.0.1:${WEBUI_PORT}/health" >/dev/null 2>&1 && break
  sleep 5
done
if curl -sf "http://127.0.0.1:${WEBUI_PORT}/health" >/dev/null 2>&1; then
  WEBUI=OK
  pass "Open WebUI answering on 127.0.0.1:${WEBUI_PORT}"
else
  WEBUI=FAILED
  fail "Open WebUI did not come up. Last log lines:"
  sudo docker logs --tail 40 "$WEBUI_NAME"
  finish ABORT_WEBUI 1
fi

# 9. Confirm nothing is listening on a public address
if sudo ss -ltn | awk '{print $4}' | grep -Eq '^(0\.0\.0\.0|\[::\]|\*):(11434|'"$WEBUI_PORT"')$'; then
  BIND=PUBLIC_CHECK
  warn "Ollama or the chat app is listening on all addresses. The security group still blocks it, but check: sudo ss -ltnp"
else
  BIND=LOCALHOST_ONLY
  pass "Ollama and the chat app listen on localhost only"
fi

echo ""
echo "Chat window: open http://localhost:${WEBUI_PORT} on your own computer while the SSH tunnel is open."
echo "Terminal fallback on this box: ollama run $MODEL"
if [ "$INFER" = "OK" ] && echo "$GPU_SPLIT" | grep -q '100%_GPU'; then
  finish READY 0
else
  finish READY_CHECK_GPU 0
fi
