"""li-auth: invites, Face ID passkeys and the voice relay for Li.
Runs inside the open-webui container (docker exec) so it can use Open WebUI's own
account code. Listens on 127.0.0.1 only; Caddy sends /li-auth/* here.
Nothing here stores a password: Open WebUI keeps those. Invite codes are stored hashed."""
import logging; logging.getLogger('httpx').setLevel(logging.WARNING)
import asyncio, base64, hashlib, json, os, secrets, sqlite3, sys, threading, time
from datetime import timedelta

sys.path.insert(1, os.environ.get('LI_BACKEND', '/app/backend'))  # Open WebUI's own package
sys.path.append(os.environ.get('LI_PYLIB', '/app/backend/data/li/pylib'))
try:
    open('/tmp/li_auth.pid', 'w').write(str(os.getpid()))
except Exception:
    pass

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from webauthn import (generate_registration_options, verify_registration_response,
                      generate_authentication_options, verify_authentication_response, options_to_json)
from webauthn.helpers import bytes_to_base64url, base64url_to_bytes
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, ResidentKeyRequirement,
                                      UserVerificationRequirement, PublicKeyCredentialDescriptor)

OWUI = os.environ.get('LI_OWUI', 'http://127.0.0.1:3000')
TTS = os.environ.get('LI_TTS', 'http://127.0.0.1:3012')
PORT = int(os.environ.get('LI_PORT', '3010'))
RP_ID = os.environ.get('LI_RP_ID', 'chat.zeiddata.com')
ORIGIN = os.environ.get('LI_ORIGIN', 'https://' + RP_ID)
DB = os.environ.get('LI_DB', '/app/backend/data/li/li_auth.db')
ALPHA = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'

os.makedirs(os.path.dirname(DB), exist_ok=True)
_db = sqlite3.connect(DB, check_same_thread=False)
_db.row_factory = sqlite3.Row
_lk = threading.Lock()
with _lk:
    _db.executescript('''
    create table if not exists invites(id text primary key, code_hash text unique, label text, email text,
      created_by text, created_at integer, expires_at integer, revoked integer default 0,
      claimed_until integer default 0, used_at integer, used_by text, used_email text);
    create table if not exists passkeys(id text primary key, user_id text, public_key text, sign_count integer,
      name text, created_at integer, last_used integer);
    create index if not exists pk_user on passkeys(user_id);
    ''')
    _db.commit()

def q(sql, args=(), one=False):
    with _lk:
        cur = _db.execute(sql, args); _db.commit(); rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows

now = lambda: int(time.time())
H = lambda code: hashlib.sha256(code.strip().upper().replace('-', '').replace(' ', '').encode()).hexdigest()
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
http = httpx.AsyncClient(timeout=30)

# ---------- Open WebUI's own account code (same container, same secret) ----------
from open_webui.utils.auth import create_token
from open_webui.models.auths import Auths
from open_webui.models.users import Users
try:
    from open_webui.utils.misc import parse_duration
except Exception:
    from open_webui.utils.auth import parse_duration
try:
    from open_webui.models.config import Config
except Exception:
    Config = None

async def mint(user_id: str, minutes: int | None = None) -> str:
    auth = await Auths.get_auth_by_id(user_id)
    if not auth or not getattr(auth, 'active', True):
        raise HTTPException(403, 'That account is switched off.')
    if minutes:
        exp = timedelta(minutes=minutes)
    else:
        raw = '4w'
        try:
            raw = (await Config.get('auth.jwt_expiry')) if Config else raw
        except Exception:
            pass
        exp = parse_duration(raw or '4w')
    return create_token(data={'id': user_id, 'typ': 'session', 'session_stamp': auth.session_stamp}, expires_delta=exp)

# ---------- helpers ----------
_who = {}
async def who(req: Request, need=('user', 'admin')) -> dict:
    a = req.headers.get('authorization', '')
    if not a.lower().startswith('bearer '):
        raise HTTPException(401, 'Sign in first.')
    k = hashlib.sha256(a.encode()).hexdigest()
    hit = _who.get(k)
    if hit and hit[0] > time.time():
        u = hit[1]
    else:
        r = await http.get(OWUI + '/api/v1/auths/', headers={'Authorization': a})
        if r.status_code != 200:
            raise HTTPException(401, 'Sign in first.')
        u = r.json(); _who[k] = (time.time() + 60, u)
        if len(_who) > 500: _who.clear()
    if u.get('role') not in need:
        raise HTTPException(403, 'Your account is waiting for approval.' if u.get('role') == 'pending' else 'Admins only.')
    return u

_hits = {}
def limit(req: Request, bucket: str, n=20, window=300):
    ip = (req.headers.get('x-forwarded-for') or (req.client.host if req.client else '?')).split(',')[0].strip()
    k = bucket + ip; t = time.time()
    xs = [x for x in _hits.get(k, []) if x > t - window]
    if len(xs) >= n:
        raise HTTPException(429, 'Too many tries. Wait a few minutes.')
    xs.append(t); _hits[k] = xs

def invite_status(r):
    if r['used_at']: return 'used'
    if r['revoked']: return 'revoked'
    if r['expires_at'] < now(): return 'expired'
    return 'waiting'

def live_invite(code: str):
    r = q('select * from invites where code_hash=?', (H(code),), one=True)
    if not r or invite_status(r) != 'waiting':
        raise HTTPException(404, "That invite isn't valid. Ask for a new one.")
    return r

@app.get('/li-auth/health')
async def health():
    return {'ok': True, 'rp_id': RP_ID}

# ---------- invites ----------
@app.post('/li-auth/invites')
async def make_invite(req: Request):
    u = await who(req, ('admin',)); b = await req.json()
    label = str(b.get('label') or '').strip()[:80]
    email = str(b.get('email') or '').strip().lower()[:200] or None
    days = max(1, min(30, int(b.get('days') or 7)))
    raw = ''.join(secrets.choice(ALPHA) for _ in range(10)); code = raw[:5] + '-' + raw[5:]
    iid = secrets.token_hex(6)
    q('insert into invites(id,code_hash,label,email,created_by,created_at,expires_at) values(?,?,?,?,?,?,?)',
      (iid, H(code), label, email, u['id'], now(), now() + days * 86400))
    return {'id': iid, 'code': code, 'link': f'{ORIGIN}/?invite={code}', 'label': label, 'email': email, 'expires_at': now() + days * 86400}

@app.get('/li-auth/invites')
async def list_invites(req: Request):
    await who(req, ('admin',))
    rows = q('select * from invites order by created_at desc limit 100')
    return [{'id': r['id'], 'label': r['label'], 'email': r['email'], 'created_at': r['created_at'],
             'expires_at': r['expires_at'], 'used_at': r['used_at'], 'used_email': r['used_email'],
             'status': invite_status(r)} for r in rows]

@app.delete('/li-auth/invites/{iid}')
async def revoke_invite(iid: str, req: Request):
    await who(req, ('admin',))
    q('update invites set revoked=1 where id=? and used_at is null', (iid,))
    return {'ok': True}

@app.post('/li-auth/invite/check')
async def check_invite(req: Request):
    limit(req, 'chk', 30); b = await req.json()
    r = live_invite(str(b.get('code') or ''))
    return {'ok': True, 'label': r['label'], 'email': r['email']}

@app.post('/li-auth/invite/redeem')
async def redeem(req: Request):
    limit(req, 'red', 12); b = await req.json()
    code = str(b.get('code') or ''); name = str(b.get('name') or '').strip()[:80]
    email = str(b.get('email') or '').strip().lower()[:200]; pw = str(b.get('password') or '')
    if not name or not email or not pw:
        raise HTTPException(400, 'Fill in your name, email and a password.')
    r = live_invite(code)
    if r['email'] and r['email'] != email:
        raise HTTPException(400, 'This invite is for a different email address.')
    t = now()
    with _lk:
        cur = _db.execute('update invites set claimed_until=? where id=? and used_at is null and claimed_until<?', (t + 60, r['id'], t)); _db.commit()
        got = cur.rowcount == 1
    if not got:
        raise HTTPException(409, 'That invite is being used right now. Try again in a minute.')
    try:
        inviter = await Users.get_user_by_id(r['created_by'])
        if not inviter or inviter.role != 'admin':
            raise HTTPException(403, 'Whoever made this invite is no longer an admin. Ask for a new one.')
        admin_tok = await mint(inviter.id, minutes=2)
        res = await http.post(OWUI + '/api/v1/auths/add', headers={'Authorization': 'Bearer ' + admin_tok},
                              json={'name': name, 'email': email, 'password': pw, 'role': 'user'})
        j = res.json() if res.headers.get('content-type', '').startswith('application/json') else {}
        if res.status_code != 200 or not j.get('id'):
            raise HTTPException(400, str(j.get('detail') or 'Could not make the account.')[:200])
        q('update invites set used_at=?, used_by=?, used_email=? where id=?', (now(), j['id'], email, r['id']))
        tok = j.get('token') or await mint(j['id'])
        return {'token': tok, 'id': j['id'], 'name': j.get('name'), 'role': j.get('role')}
    finally:
        q('update invites set claimed_until=0 where id=? and used_at is null', (r['id'],))

# ---------- passkeys (Face ID / Touch ID / phone screen lock) ----------
_ch = {}
def put_ch(key, val):
    t = time.time()
    for k in [k for k, v in _ch.items() if v[0] < t]: _ch.pop(k, None)
    _ch[key] = (t + 180, val)
def take_ch(key):
    v = _ch.pop(key, None)
    if not v or v[0] < time.time():
        raise HTTPException(400, 'That took too long. Try again.')
    return v[1]

@app.post('/li-auth/passkey/register/options')
async def reg_options(req: Request):
    u = await who(req)
    mine = q('select id from passkeys where user_id=?', (u['id'],))
    o = generate_registration_options(
        rp_id=RP_ID, rp_name='Li', user_id=u['id'].encode(), user_name=u.get('email') or u['id'],
        user_display_name=u.get('name') or 'Li',
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(r['id'])) for r in mine],
        authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.REQUIRED,
                                                               user_verification=UserVerificationRequirement.REQUIRED))
    put_ch('reg' + u['id'], o.challenge)
    return JSONResponse(json.loads(options_to_json(o)))

@app.post('/li-auth/passkey/register/verify')
async def reg_verify(req: Request):
    u = await who(req); b = await req.json()
    ch = take_ch('reg' + u['id'])
    try:
        v = verify_registration_response(credential=b['credential'], expected_challenge=ch, expected_rp_id=RP_ID,
                                         expected_origin=ORIGIN, require_user_verification=True)
    except Exception as e:
        raise HTTPException(400, 'Face ID setup failed: ' + str(e)[:120])
    cid = bytes_to_base64url(v.credential_id)
    q('insert or replace into passkeys(id,user_id,public_key,sign_count,name,created_at,last_used) values(?,?,?,?,?,?,?)',
      (cid, u['id'], base64.b64encode(v.credential_public_key).decode(), v.sign_count, str(b.get('name') or 'This device')[:60], now(), now()))
    return {'ok': True, 'id': cid}

@app.get('/li-auth/passkeys')
async def my_passkeys(req: Request):
    u = await who(req)
    return [{'id': r['id'], 'name': r['name'], 'created_at': r['created_at'], 'last_used': r['last_used']}
            for r in q('select * from passkeys where user_id=? order by created_at', (u['id'],))]

@app.delete('/li-auth/passkeys/{pid}')
async def drop_passkey(pid: str, req: Request):
    u = await who(req)
    q('delete from passkeys where id=? and user_id=?', (pid, u['id']))
    return {'ok': True}

@app.post('/li-auth/passkey/login/options')
async def login_options(req: Request):
    limit(req, 'plo', 40)
    o = generate_authentication_options(rp_id=RP_ID, user_verification=UserVerificationRequirement.REQUIRED)
    cid = secrets.token_urlsafe(16); put_ch('login' + cid, o.challenge)
    return {'challenge_id': cid, 'options': json.loads(options_to_json(o))}

@app.post('/li-auth/passkey/login/verify')
async def login_verify(req: Request):
    limit(req, 'plv', 20); b = await req.json()
    ch = take_ch('login' + str(b.get('challenge_id') or ''))
    cred = b.get('credential') or {}
    row = q('select * from passkeys where id=?', (str(cred.get('id') or ''),), one=True)
    if not row:
        raise HTTPException(404, "This Face ID key isn't set up for Li. Sign in with your password, then turn Face ID on.")
    uh = (cred.get('response') or {}).get('userHandle')
    if uh and base64url_to_bytes(uh).decode(errors='replace') != row['user_id']:
        raise HTTPException(400, "That key doesn't match the account.")
    try:
        v = verify_authentication_response(credential=cred, expected_challenge=ch, expected_rp_id=RP_ID, expected_origin=ORIGIN,
                                           credential_public_key=base64.b64decode(row['public_key']),
                                           credential_current_sign_count=row['sign_count'], require_user_verification=True)
    except Exception as e:
        raise HTTPException(400, 'Face ID sign-in failed: ' + str(e)[:120])
    user = await Users.get_user_by_id(row['user_id'])
    if not user:
        q('delete from passkeys where user_id=?', (row['user_id'],))
        raise HTTPException(404, 'That account was removed.')
    if user.role == 'pending':
        raise HTTPException(403, 'Your account is waiting for approval.')
    q('update passkeys set sign_count=?, last_used=? where id=?', (v.new_sign_count, now(), row['id']))
    return {'token': await mint(user.id), 'id': user.id, 'name': user.name, 'role': user.role}


# ---------- web: private search (SearXNG on 127.0.0.1) and a page reader ----------
import ipaddress, re as _re, socket
from urllib.parse import urlparse, urljoin
SEARX = os.environ.get('LI_SEARX', 'http://127.0.0.1:8888')
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36'
web_http = httpx.AsyncClient(timeout=15, follow_redirects=False, headers={'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.8'})

def public_url(u: str) -> str:
    p = urlparse(u)
    if p.scheme not in ('http', 'https') or not p.hostname:
        raise HTTPException(400, 'Only web addresses (http or https) can be read.')
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == 'https' else 80), proto=socket.IPPROTO_TCP)
    except Exception:
        raise HTTPException(400, "That site doesn't resolve.")
    for i in infos:
        ip = ipaddress.ip_address(i[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise HTTPException(400, "Li doesn't read private or internal addresses.")
    return u

def page_text(html: str):
    try:
        from bs4 import BeautifulSoup
        s = BeautifulSoup(html, 'html.parser')
        for t in s(['script', 'style', 'noscript', 'svg', 'nav', 'footer', 'header', 'form', 'iframe']):
            t.decompose()
        title = (s.title.string or '').strip() if s.title and s.title.string else ''
        main = s.find('article') or s.find('main') or s.body or s
        text = main.get_text('\n')
    except Exception:
        title = (_re.search(r'<title[^>]*>(.*?)</title>', html, _re.S | _re.I) or [None, ''])[1].strip()
        text = _re.sub(r'<(script|style)[\s\S]*?</\1>', ' ', html, flags=_re.I)
        text = _re.sub(r'<[^>]+>', ' ', text)
    text = _re.sub(r'[ \t\r\f\v]+', ' ', text)
    text = _re.sub(r'\n\s*\n+', '\n\n', text).strip()
    return title, text

@app.post('/li-auth/web/search')
async def web_search(req: Request):
    await who(req); b = await req.json()
    qy = str(b.get('q') or '').strip()[:300]
    if not qy:
        raise HTTPException(400, 'Nothing to search for.')
    try:
        r = await http.get(SEARX + '/search', params={'q': qy, 'format': 'json', 'safesearch': 0, 'language': 'en'})
        j = r.json()
    except Exception:
        raise HTTPException(502, 'Web search is not answering right now.')
    out, seen = [], set()
    for x in j.get('results', []):
        u = x.get('url') or ''
        if not u.startswith('http') or u in seen:
            continue
        seen.add(u)
        out.append({'title': (x.get('title') or '')[:200], 'url': u, 'snippet': (x.get('content') or '')[:400]})
        if len(out) >= int(b.get('n') or 6):
            break
    return {'q': qy, 'results': out}

@app.post('/li-auth/web/read')
async def web_read(req: Request):
    await who(req); b = await req.json()
    u = str(b.get('url') or '').strip()
    limit_chars = max(1000, min(20000, int(b.get('chars') or 6000)))
    for _ in range(5):
        public_url(u)
        try:
            r = await web_http.get(u)
        except Exception:
            raise HTTPException(502, "Couldn't reach that page.")
        if r.is_redirect and r.headers.get('location'):
            u = urljoin(u, r.headers['location']); continue
        break
    else:
        raise HTTPException(502, 'Too many redirects.')
    if r.status_code >= 400:
        raise HTTPException(502, f'The page answered {r.status_code}.')
    ct = r.headers.get('content-type', '')
    body = r.content[:3_000_000]
    if 'html' in ct or not ct:
        title, text = page_text(body.decode(r.encoding or 'utf-8', errors='replace'))
    elif ct.startswith('text/') or 'json' in ct:
        title, text = '', body.decode(r.encoding or 'utf-8', errors='replace')
    else:
        raise HTTPException(415, 'Li reads web pages and text, not that kind of file.')
    return {'url': str(r.url), 'title': title[:200], 'text': text[:limit_chars], 'chars': len(text)}

# ---------- voice ----------
@app.post('/li-auth/tts')
async def tts(req: Request):
    await who(req); b = await req.json()
    r = await http.post(TTS + '/tts', json={'text': str(b.get('text') or '')[:600], 'voice': str(b.get('voice') or 'af_heart'),
                                           'speed': float(b.get('speed') or 1.0)})
    if r.status_code != 200:
        raise HTTPException(502 if r.status_code >= 500 else r.status_code, 'Voice unavailable.')
    return Response(r.content, media_type='audio/wav', headers={'Cache-Control': 'no-store'})

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=PORT, log_level='warning')
