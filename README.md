# zd_abliteration_model

Rent-by-the-hour GPU instance running a local LLM, reachable by the operator over
SSH and optionally by one outside party over an authenticated HTTPS API. Single
operator. Start when needed, stop when idle.

## What this is

A minimal, repeatable setup to run a 20B GGUF model on an AWS g6.xlarge
(NVIDIA L4, 24 GB) via Ollama. Two layers:

- setup_heretic.sh installs Ollama and pulls the model. Local use only, over an
  SSH tunnel.
- setup_proxy.sh puts an authenticated HTTPS reverse proxy (Caddy) in front of
  Ollama so a specific outside party can reach the model with a bearer token.
  Ollama itself stays bound to localhost.
- chat-worker/ is a Cloudflare Worker serving a password-gated web chat UI that
  proxies to the box, keeping the token server-side.

## Files

- setup_heretic.sh   model install and smoke test (runs on the box)
- setup_proxy.sh     authenticated HTTPS proxy in front of Ollama (runs on the box)
- ssh_her.ps1        PowerShell shortcut to SSH in and open the API tunnel
- RUNBOOK.md         full build steps, start to finish
- HANDOFF.md         what to send an outside party who gets API access
- chat-worker/       Cloudflare Worker chat front end

## Cost

- g6.xlarge on-demand: about $0.80/hr (us-east-1, verify current)
- Running 24/7: about $590/mo. Do not.
- About 3 hr/day, stopped when idle: about $70/mo
- 80 GB gp3 disk: about $6/mo, billed even while stopped
- Elastic IP: free while attached to a running instance, about $3 to $4/mo
  while the instance is stopped

Stop the instance when done. The disk and Elastic IP persist, restart is quick.

## Security notes

- Ollama has no authentication and stays bound to localhost. Never expose port
  11434 directly.
- The proxy token is the access control. Rotate it by re-running setup_proxy.sh.
- Kill outside access instantly with: sudo systemctl stop caddy
- The model is an abliterated build with no refusal filtering. Content controls,
  if any, are the operator responsibility. Anything an outside party generates
  traces back to this infrastructure.
- Never commit keys, .pem files, tokens, or the heretic_proxy.env pointer.
