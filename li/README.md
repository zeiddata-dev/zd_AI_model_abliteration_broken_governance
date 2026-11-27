# Li front end

`index.html` is the Li chat page: a phone-first chat UI with the rust 3D Zeid Z.
`setup_li_ui.sh` (repo root) puts it at `https://chat.zeiddata.com/` behind the same private-link gate and account login as the Open WebUI chat.

How it works:

- Li is a single static page. Caddy serves it at `/`, and its assets (three.js, fonts, starter notes) at `/li/*`, all from the box.
- Everything else (`/api`, `/admin`, `/auth`) still goes to Open WebUI, which keeps handling accounts and the model.
- Sign-in uses Open WebUI accounts (`/api/v1/auths/signin`, or `signup` for the first account, which becomes admin).
- Replies stream from `/api/chat/completions`. Tokens per second come from the box's own usage numbers when present, otherwise they are measured in the browser.
- Chats, projects and case files are saved in the browser (localStorage), not on the server yet.
- Starter notes live only on the box at `/var/www/li/li/notes.json`. They are not in this repo.

Not wired yet: Lithium memory API, Lithium Live voice, PDF and spreadsheet reading, server-side chat storage, and any off-box model.
