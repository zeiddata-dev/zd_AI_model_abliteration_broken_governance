#!/usr/bin/env bash
LOG=/tmp/heretic_setup.log
PTR=/tmp/heretic_last_setup.env
MODEL=svjack/gpt-oss-20b-heretic
CTX=32768
RED=$'\033[0;31m'
GRN=$'\033[0;32m'
NC=$'\033[0m'
exec > >(tee -a "$LOG") 2>&1
pass(){ printf "%sPASS%s  %s\n" "$GRN" "$NC" "$1"; }
fail(){ printf "%sFAIL%s  %s\n" "$RED" "$NC" "$1"; }
echo "==== heretic setup start $(date -u +%FT%TZ) ===="
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
  GPU=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -n1)
  pass "GPU detected: $GPU"
else
  fail "No usable NVIDIA GPU."
  echo "DECISION=ABORT_NO_GPU"; echo "exit_code=1"; exit 1
fi
if ! command -v ollama >/dev/null 2>&1; then
  curl -fsSL https://ollama.com/install.sh | sh
fi
if command -v ollama >/dev/null 2>&1; then
  pass "Ollama installed"
else
  fail "Ollama install failed."
  echo "DECISION=ABORT_OLLAMA_INSTALL"; echo "exit_code=1"; exit 1
fi
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/override.conf >/dev/null <<OVR
[Service]
Environment="OLLAMA_CONTEXT_LENGTH=$CTX"
OVR
sudo systemctl daemon-reload
sudo systemctl restart ollama
sleep 5
if systemctl is-active --quiet ollama; then
  pass "Ollama service active, context length $CTX"
else
  fail "Ollama service not active."
  echo "DECISION=ABORT_SERVICE"; echo "exit_code=1"; exit 1
fi
ollama pull "$MODEL"
if ollama list | grep -q "$(echo "$MODEL" | cut -d: -f1)"; then
  pass "Model pulled: $MODEL"
else
  fail "Model pull failed: $MODEL"
  echo "DECISION=ABORT_PULL"; echo "exit_code=1"; exit 1
fi
RESP=$(curl -s --max-time 600 http://localhost:11434/api/generate -d "{\"model\":\"$MODEL\",\"prompt\":\"Reply with one word: online\",\"keep_alive\":\"30m\",\"stream\":false}")
if echo "$RESP" | grep -qi '"response"'; then
  pass "Inference responded"
  INFER=OK
else
  fail "Empty or malformed inference response."
  INFER=CHECK
fi
{
  echo "MODEL=$MODEL"
  echo "CTX=$CTX"
  echo "ENDPOINT=http://localhost:11434"
  echo "GPU=$GPU"
  echo "INFER=$INFER"
  echo "SETUP_UTC=$(date -u +%FT%TZ)"
} > "$PTR"
pass "Pointer written: $PTR"
echo "DECISION=READY"
echo "exit_code=0"
