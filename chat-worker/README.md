# chat-worker

Cloudflare Worker that serves a password-gated multi-turn chat UI at
chat.zeiddata.com and proxies to the model box. The box token lives in the
Worker as a secret and never reaches the browser.

## Flow

    browser (chat.zeiddata.com)
      -> Worker: serves the UI, checks the shared password
      -> box (BOX_URL/api/chat) with Authorization: Bearer BOX_TOKEN
      -> streamed reply passed back to the browser

Same origin throughout, so no CORS. The box must be running for chat to work.

## Secrets (never committed)

- BOX_URL         e.g. https://YOUR_BOX_DOMAIN
- BOX_TOKEN       the bearer token from the box heretic_proxy.env
- CHAT_PASSWORD   the shared access password you hand to users

## Deploy (wrangler CLI, needs Node.js)

    cd chat-worker
    npm install -g wrangler
    wrangler login
    wrangler secret put BOX_URL
    wrangler secret put BOX_TOKEN
    wrangler secret put CHAT_PASSWORD
    wrangler deploy

custom_domain = true makes Cloudflare provision DNS and the certificate for
chat.zeiddata.com automatically (the zone must already be on Cloudflare).

## Deploy (dashboard alternative, no CLI)

1. Cloudflare dashboard, Workers and Pages, Create, Worker.
2. Paste the contents of worker.js, Save and Deploy.
3. Settings, Variables, add three Secrets: BOX_URL, BOX_TOKEN, CHAT_PASSWORD.
4. Settings, Domains and Routes, add Custom Domain chat.zeiddata.com.

## Rotate / revoke

- Change the shared password: wrangler secret put CHAT_PASSWORD again.
- Rotate the box token: re-run setup_proxy.sh on the box, then
  wrangler secret put BOX_TOKEN with the new value.
- Kill all web access: delete the Worker, or stop Caddy on the box.

## Notes

- Streaming uses Ollama /api/chat NDJSON. The model reasoning is shown dimmed.
- Password is checked server-side in the Worker, travels over HTTPS, held only
  in the browser sessionStorage for the session.
- Shared-password gate, not per-user auth. For per-user revocation, switch to
  per-user tokens later.
