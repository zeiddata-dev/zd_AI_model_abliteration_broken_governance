# Handoff: model API access

What to send an outside party who gets API access. Replace SUBDOMAIN.YOURDOMAIN
and PASTE_TOKEN_HERE. Send the token over a private channel, never email or plain
chat.

## Connection

    Endpoint:  https://SUBDOMAIN.YOURDOMAIN/api/generate
    Chat:      https://SUBDOMAIN.YOURDOMAIN/api/chat
    Model:     svjack/gpt-oss-20b-heretic
    Auth:      Authorization: Bearer PASTE_TOKEN_HERE

The endpoint only responds while the host is running. First request after startup
is slow (model loading), then fast.

## Single-shot prompt (stateless)

curl (Mac/Linux):

    curl https://SUBDOMAIN.YOURDOMAIN/api/generate \
      -H "Authorization: Bearer PASTE_TOKEN_HERE" \
      -d "{\"model\":\"svjack/gpt-oss-20b-heretic\",\"prompt\":\"your prompt\",\"stream\":false}"

Python:

    import requests
    r = requests.post(
        "https://SUBDOMAIN.YOURDOMAIN/api/generate",
        headers={"Authorization": "Bearer PASTE_TOKEN_HERE"},
        json={"model": "svjack/gpt-oss-20b-heretic", "prompt": "your prompt", "stream": False},
    )
    print(r.json()["response"])

Windows PowerShell (use this, not curl):

    $headers = @{ Authorization = "Bearer PASTE_TOKEN_HERE" }
    $body = "{\"model\":\"svjack/gpt-oss-20b-heretic\",\"prompt\":\"your prompt\",\"stream\":false}"
    Invoke-RestMethod -Uri https://SUBDOMAIN.YOURDOMAIN/api/generate -Method Post -Headers $headers -Body $body -ContentType "application/json"

## Multi-turn chat (with memory)

/api/generate is stateless. For a real back-and-forth, use /api/chat and send the
full message history each turn, appending each reply before the next question.

## Chat app (easiest, no code)

The endpoint speaks the Ollama API. Any app supporting a custom Ollama server AND
custom headers works (e.g. Chatbox: set API host to the endpoint, add header
Authorization = Bearer PASTE_TOKEN_HERE, model svjack/gpt-oss-20b-heretic).

## Streaming

Set stream true to receive tokens as they generate. The response arrives as one
JSON object per line.
