# Runbook: zd_abliteration_model

End-to-end build of a private LLM on a rented GPU, with authenticated HTTPS
access for one outside party, plus an optional Cloudflare Worker chat front end.
Replace every placeholder in ALL_CAPS with your real values. Do not commit real
tokens, IPs, or keys.

## 0. Prerequisites

- AWS account with quota for "Running On-Demand G and VT instances" >= 4 vCPU,
  in the region you will use. New accounts are often denied the first request;
  reopen the case with a specific single-instance use case.
- A domain you control (for the HTTPS proxy and the chat subdomain).

## 1. Launch the instance

- Region: pick one and keep it. Quota, AMI, and instance all live in one region.
- AMI: Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04), owner Amazon.
  Not in Quick Start; find it under Community AMIs (owner alias amazon), or get
  the exact ID from CloudShell:
      aws ssm get-parameter --region YOUR_REGION --name /aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id --query "Parameter.Value" --output text
- Instance type: g6.xlarge
- Key pair: create new, RSA, .pem, download and keep it. You get it once.
- Storage: 80 GB gp3
- Security group: SSH (22) from My IP only.

## 2. Pin an Elastic IP

- EC2 > Elastic IPs > Allocate, then Associate to the instance.
- Survives stop/start, so the SSH shortcut and DNS never break.
- If your network changes and SSH times out, edit the security group SSH rule
  and reset Source to My IP.

## 3. Install the model

SSH in, create setup_heretic.sh on the box, run it. Expect DECISION=READY. First
model load is slow (cold, ~15 GB to GPU), then fast. Verify the GPU holds it:
    nvidia-smi --query-gpu=name,memory.used,utilization.gpu --format=csv,noheader
memory.used in the thousands of MiB means the model is resident on the GPU.

## 4. Local access (SSH tunnel)

Windows PowerShell: because Windows Terminal / OneDrive can lock the profile,
put the function in a standalone file and load it from the profile:
    Set-Content -Path "$HOME\.ssh\ssh_her.ps1" -Value 'function ssh_her { ssh -i "$HOME\.ssh\gpu-box-key.pem" -L 11434:localhost:11434 ubuntu@YOUR_ELASTIC_IP }'
Then `ssh_her` connects and opens the tunnel. If a fresh window says "not
recognized", run `. "$HOME\.ssh\ssh_her.ps1"` first.
Note: PowerShell aliases curl to Invoke-WebRequest. Use curl.exe or, better,
Invoke-RestMethod for API calls.

## 5. Outside access over authenticated HTTPS (optional)

1. DNS: A record SUBDOMAIN.YOURDOMAIN -> YOUR_ELASTIC_IP, low TTL (300).
   Confirm it resolves before continuing.
2. Security group: add inbound HTTP (80) and HTTPS (443) from Anywhere-IPv4.
   Port 80 must be world-open for the certificate to issue and renew. The token,
   not the firewall, is the access control.
3. On the box: set DOMAIN at the top of setup_proxy.sh, run it, expect
   DECISION=READY. It prints the endpoint and token.

Design notes from the build:
- Ollama rejects proxied requests unless origins are allowed and the upstream
  Host looks local. setup_proxy.sh sets OLLAMA_ORIGINS=* and Caddy sends
  header_up Host localhost:11434.
- Caddy auth uses a positive match (@authorized header Authorization "Bearer
  TOKEN") with a handle/fallthrough to 403, not a negated matcher. The negated
  form misfires and 403s valid tokens.

## 6. Verify the proxy

With token -> 200 + JSON. Without token -> 403 Unauthorized. From PowerShell use
Invoke-RestMethod (raw curl JSON quoting is unreliable there).

## 7. Web chat front end (optional)

See chat-worker/. A Cloudflare Worker serves a password-gated chat UI at
chat.zeiddata.com and proxies to the box with the token held server-side.
Secrets: BOX_URL, BOX_TOKEN, CHAT_PASSWORD. Deploy with wrangler or the Cloudflare
dashboard.

## 8. Daily operation

- Start the instance when needed. Elastic IP and DNS stay valid.
- First request after start is slow (cold load), then fast.
- Stop the instance when done. Main cost lever.
- Outside access (proxy and chat) only works while the box is running.

## 9. Revoke access

    sudo systemctl stop caddy      # kill the API endpoint immediately
    # re-run setup_proxy.sh for a fresh token (old token stops working)
    # for chat: rotate CHAT_PASSWORD in the Worker, or delete the Worker

## Troubleshooting quick reference

- SSH times out: instance stopped, Elastic IP not associated, or your IP changed
  (reset SSH source to My IP).
- Proxy empty 403 with Via: Caddy: Ollama rejecting the proxied Host. Confirm
  OLLAMA_ORIGINS=* and the header_up Host line.
- Proxy 403 with Server: Caddy and body Unauthorized: token mismatch or no token.
- 400 "invalid character 'm'": malformed JSON body, usually PowerShell quoting.
  Use Invoke-RestMethod.
- Cert fails to issue: DNS not resolving yet, or port 80 not open. Check
  sudo journalctl -u caddy --no-pager -n 30.
