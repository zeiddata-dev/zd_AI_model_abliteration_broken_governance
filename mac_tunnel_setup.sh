#!/usr/bin/env bash
# mac_tunnel_setup.sh
# Run once on your Mac after the EC2 box is up. Adds an SSH shortcut named "heretic"
# that logs in AND opens the chat tunnel (localhost:3000) plus the raw API (localhost:11434).
# After this, the only command you need is:  ssh heretic

TS=$(date +%Y%m%d_%H%M%S)
LOG=/tmp/heretic_mac_tunnel_${TS}.log
CFG="$HOME/.ssh/config"
KEY="$HOME/.ssh/gpu-box-key.pem"
BACKUP="$HOME/.ssh/config.bak_${TS}"
exec > >(tee -a "$LOG") 2>&1

cd "$HOME" || exit 1
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"

if [ ! -f "$KEY" ] && [ -f "$HOME/Downloads/gpu-box-key.pem" ]; then
  mv "$HOME/Downloads/gpu-box-key.pem" "$KEY"
  echo "Moved key from Downloads to $KEY"
fi
if [ ! -f "$KEY" ]; then
  echo "FAIL  Key not found at $KEY or ~/Downloads/gpu-box-key.pem"
  echo "DECISION=ABORT_NO_KEY"
  echo "exit_code=1"
  exit 1
fi
chmod 400 "$KEY"

read -r -p "Elastic IP of the GPU box: " BOX_IP
if ! echo "$BOX_IP" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "FAIL  That does not look like an IP address: $BOX_IP"
  echo "DECISION=ABORT_BAD_IP"
  echo "exit_code=1"
  exit 1
fi

touch "$CFG"
cp -p "$CFG" "$BACKUP"
echo "Backed up $CFG to $BACKUP"

# Remove any old "heretic" block, then append the new one
awk 'BEGIN{skip=0} /^Host heretic$/{skip=1; next} /^Host /{skip=0} skip==0{print}' "$BACKUP" > "$CFG"
cat >> "$CFG" <<EOF

Host heretic
    HostName $BOX_IP
    User ubuntu
    IdentityFile $KEY
    IdentitiesOnly yes
    LocalForward 3000 localhost:3000
    LocalForward 11434 localhost:11434
    ServerAliveInterval 30
    ExitOnForwardFailure yes
EOF
chmod 600 "$CFG"

if ssh -G heretic | grep -q "hostname $BOX_IP"; then
  echo "PASS  Shortcut added. Connect with: ssh heretic"
  echo "PASS  While connected, open http://localhost:3000 in your browser."
  echo "DECISION=READY"
  echo "exit_code=0"
else
  echo "FAIL  ssh did not pick up the new block. Restore with: cp $BACKUP $CFG"
  echo "DECISION=CHECK_CONFIG"
  echo "exit_code=1"
fi
