"""li-auth: invites, Face ID passkeys and the voice relay for Li.
Runs inside the open-webui container (docker exec) so it can use Open WebUI's own
account code. Listens on 127.0.0.1 only; Caddy sends /li-auth/* here.
Nothing here stores a password: Open WebUI keeps those. Invite codes are stored hashed."""
import logging; logging.getLogger('httpx').setLevel(logging.WARNING)
import asyncio, base64, hashlib, ipaddress, json, os, re, secrets, sqlite3, sys, threading, time
from datetime import timedelta

sys.path.insert(1, os.environ.get('LI_BACKEND', '/app/backend'))  # Open WebUI's own package
sys.path.append(os.environ.get('LI_PYLIB', '/app/backend/data/li/pylib'))
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


# ---------- case workspace: evidence log (SHA-256), entities, timeline ----------
OSINT = os.environ.get('LI_OSINT', 'http://127.0.0.1:3014')
with _lk:
    _db.executescript("""
    create table if not exists evidence(id integer primary key autoincrement, user_id text, case_name text, kind text, target text,
      summary text, data text, sha256 text, file_sha256 text, source text, tags text default '', created_at integer);
    create index if not exists ev_case on evidence(user_id, case_name, created_at);
    create table if not exists entities(id integer primary key autoincrement, user_id text, case_name text, etype text, value text,
      note text default '', tags text default '', first_seen integer, last_seen integer, evidence text default '',
      unique(user_id, case_name, etype, value));
    create table if not exists notes(id integer primary key autoincrement, user_id text, case_name text, text text, at integer, created_at integer);
    create table if not exists connectors(user_id text, name text, enc text, last4 text, updated_at integer, primary key(user_id, name));
    """); _db.commit()

def canon(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str)

def entity(uid, case, et, val, ev_id, t):
    val = str(val).strip()[:300]
    if not val: return
    with _lk:
        _db.execute("insert into entities(user_id,case_name,etype,value,first_seen,last_seen,evidence) values(?,?,?,?,?,?,?) "
                    "on conflict(user_id,case_name,etype,value) do update set last_seen=excluded.last_seen, "
                    "evidence=case when instr(','||entities.evidence||',', ','||excluded.evidence||',')>0 then entities.evidence else entities.evidence||','||excluded.evidence end",
                    (uid, case, et, val, t, t, str(ev_id))); _db.commit()

def extract(kind, target, data):
    out = []
    if kind in ('domain', 'harvest', 'hunter'): out.append(('domain', target))
    if kind == 'domain':
        for ip in (data.get('dns') or {}).get('A', []) + (data.get('dns') or {}).get('AAAA', []): out.append(('ip', ip))
        for e in (data.get('whois') or {}).get('emails', []) if isinstance((data.get('whois') or {}).get('emails'), list) else []: out.append(('email', e))
        org = (data.get('whois') or {}).get('org')
        if org: out.append(('organization', org))
        for n in (data.get('crtsh') or {}).get('names', [])[:60]: out.append(('domain', n))
    if kind == 'harvest':
        out += [('domain', s) for s in data.get('subdomains', [])[:150]] + [('email', e) for e in data.get('emails', [])]
    if kind in ('ip', 'shodan'): out.append(('ip', target))
    if kind == 'ip':
        if data.get('reverse_dns'): out.append(('domain', data['reverse_dns']))
        if (data.get('rdap') or {}).get('name'): out.append(('organization', data['rdap']['name']))
    if kind == 'username':
        out.append(('username', target)); out += [('account', f"{x['site']}: {x['url']}") for x in data.get('found', [])]
    if kind == 'exif' and data.get('gps'): out.append(('location', f"{data['gps']['lat']:.6f},{data['gps']['lon']:.6f}"))
    if kind == 'hunter': out += [('email', e.get('value')) for e in (data.get('emails') or []) if e.get('value')]
    if kind == 'pwned': out.append(('email', target))
    return out

def summarize(kind, target, d):
    if kind == 'domain':
        w, dn, c, wb = d.get('whois') or {}, d.get('dns') or {}, d.get('crtsh') or {}, d.get('wayback') or {}
        return '; '.join(x for x in [f"registrar {w.get('registrar')}" if w.get('registrar') else '', f"created {str(w.get('creation_date'))[:10]}" if w.get('creation_date') else '',
            f"A {', '.join(dn.get('A', [])[:4])}" if dn.get('A') else '', f"{len(c.get('names', []))} names in {c.get('certificates', 0)} certificates" if 'names' in c else '',
            f"archived since {(wb.get('first') or {}).get('when', '')[:4]}" if wb.get('first') else ''] if x) or 'no records found'
    if kind == 'ip': return f"{(d.get('rdap') or {}).get('name') or 'unknown network'}, {(d.get('rdap') or {}).get('country') or '?'}; reverse DNS {d.get('reverse_dns') or 'none'}"
    if kind == 'wayback' and d.get('error'): return d['error']
    if kind == 'wayback': return f"{d.get('scope') or ''}: {d.get('yearly_captures', 0)} yearly captures, first {((d.get('first') or {}).get('when') or '')[:8]}, last {((d.get('last') or {}).get('when') or '')[:8]}"
    if kind == 'username': return f"{len(d.get('found', []))} public profiles answered out of {d.get('checked_sites')} sites checked"
    if kind == 'harvest': return f"{len(d.get('subdomains', []))} subdomains, {len(d.get('emails', []))} emails"
    if kind == 'exif': return ', '.join(f'{k}: {v}' for k, v in (d.get('summary') or {}).items()) + (f"; GPS {d['gps']['lat']:.5f},{d['gps']['lon']:.5f}" if d.get('gps') else '; no GPS')
    if kind in ('cases', 'dockets', 'fedreg', 'ecfr', 'fsearch'): return f"{len(d.get('results', []))} results"
    if kind == 'shodan': return f"{len(d.get('ports', []))} open ports seen by Shodan: {', '.join(str(p) for p in d.get('ports', [])[:12])}"
    if kind == 'hunter': return f"{len(d.get('emails', []))} emails, pattern {d.get('pattern') or 'unknown'}"
    if kind == 'pwned': return f"{len(d.get('breaches', []))} breaches" if d.get('breaches') else 'not found in any breach'
    if kind in ('scrape', 'crawl'): return f"{d.get('pages', 1)} page(s) read"
    return ''

def save_evidence(u, case, kind, target, data, source, file_sha=None):
    t = now(); body = canon(data); sha = hashlib.sha256(body.encode()).hexdigest(); summ = summarize(kind, target, data)
    with _lk:
        cur = _db.execute('insert into evidence(user_id,case_name,kind,target,summary,data,sha256,file_sha256,source,created_at) values(?,?,?,?,?,?,?,?,?,?)',
                          (u['id'], case, kind, target, summ, body, sha, file_sha, source, t)); _db.commit(); eid = cur.lastrowid
    for et, val in extract(kind, target, data): entity(u['id'], case, et, val, eid, t)
    return {'id': eid, 'sha256': sha, 'file_sha256': file_sha, 'summary': summ, 'created_at': t, 'source': source}

def case_of(b):
    return (str(b.get('case') or 'General').strip() or 'General')[:80]

# ---------- connectors: user's own API keys, encrypted at rest ----------
from cryptography.fernet import Fernet
_kf = os.path.join(os.path.dirname(DB), 'connectors.key')
if not os.path.exists(_kf):
    with open(os.open(_kf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as f: f.write(Fernet.generate_key())
FERNET = Fernet(open(_kf, 'rb').read())
CONNECTORS = {
  'firecrawl': {'name': 'Firecrawl', 'what': 'Scrape any page to clean text, web search with full page content, and crawl a whole site.', 'key_url': 'https://www.firecrawl.dev/app/api-keys'},
  'courtlistener': {'name': 'CourtListener', 'what': 'Case law, PACER dockets in RECAP, judges and oral arguments. Works without a key; a free token raises the limits.', 'key_url': 'https://www.courtlistener.com/profile/api/'},
  'shodan': {'name': 'Shodan', 'what': 'What a public IP exposes: open ports, services, banners and known vulnerabilities, from Shodan\'s own scans.', 'key_url': 'https://account.shodan.io/'},
  'hunter': {'name': 'Hunter.io', 'what': 'Public email addresses and the email pattern for a company domain, with sources.', 'key_url': 'https://hunter.io/api-keys'},
  'hibp': {'name': 'Have I Been Pwned', 'what': 'Which known data breaches an email address appears in.', 'key_url': 'https://haveibeenpwned.com/API/Key'},
}
def key_for(uid, name):
    r = q('select enc from connectors where user_id=? and name=?', (uid, name), one=True)
    return FERNET.decrypt(r['enc'].encode()).decode() if r else None

async def check_key(name, k):
    try:
        if name == 'firecrawl':
            r = await web_http.get('https://api.firecrawl.dev/v2/team/credit-usage', headers={'Authorization': 'Bearer ' + k})
        elif name == 'courtlistener':
            r = await web_http.get('https://www.courtlistener.com/api/rest/v4/search/', params={'q': 'test'}, headers={'Authorization': 'Token ' + k})
        elif name == 'shodan':
            r = await web_http.get('https://api.shodan.io/api-info', params={'key': k})
        elif name == 'hunter':
            r = await web_http.get('https://api.hunter.io/v2/account', params={'api_key': k})
        elif name == 'hibp':
            r = await web_http.get('https://haveibeenpwned.com/api/v3/subscription/status', headers={'hibp-api-key': k, 'user-agent': 'Li-case-tools'})
        else: return False
    except Exception:
        raise HTTPException(502, 'Could not reach the service to check the key. Try again.')
    if r.status_code in (401, 403): raise HTTPException(400, 'The service turned that key down.')
    return r.status_code < 400 or r.status_code == 404

@app.get('/li-auth/connectors')
async def list_connectors(req: Request):
    u = await who(req)
    mine = {r['name']: r for r in q('select name,last4,updated_at from connectors where user_id=?', (u['id'],))}
    return [{'id': k, **v, 'connected': k in mine, 'last4': mine[k]['last4'] if k in mine else None, 'updated_at': mine[k]['updated_at'] if k in mine else None} for k, v in CONNECTORS.items()]

@app.post('/li-auth/connectors/{name}')
async def set_connector(name: str, req: Request):
    u = await who(req); limit(req, 'ck', 20)
    if name not in CONNECTORS: raise HTTPException(404, 'Unknown connector.')
    k = str((await req.json()).get('key') or '').strip()
    if not (8 <= len(k) <= 300) or any(c.isspace() for c in k): raise HTTPException(400, 'Paste the whole key, with no spaces.')
    await check_key(name, k)
    q('insert or replace into connectors(user_id,name,enc,last4,updated_at) values(?,?,?,?,?)', (u['id'], name, FERNET.encrypt(k.encode()).decode(), k[-4:], now()))
    return {'ok': True, 'last4': k[-4:]}

@app.delete('/li-auth/connectors/{name}')
async def drop_connector(name: str, req: Request):
    u = await who(req); q('delete from connectors where user_id=? and name=?', (u['id'], name)); return {'ok': True}

# ---------- tools: OSINT (li-osint service), legal (free public APIs), connectors (user's keys) ----------
_heavy = asyncio.Semaphore(2)
HEAVY = {'username', 'harvest', 'crawl'}
async def osint_call(path, b, timeout=60):
    try:
        r = await http.post(OSINT + path, json=b, timeout=timeout)
    except Exception:
        raise HTTPException(502, 'The OSINT tools are not answering right now.')
    if r.status_code >= 400:
        try: d = r.json().get('detail')
        except Exception: d = None
        raise HTTPException(r.status_code if r.status_code < 500 else 502, d or 'The tool failed.')
    return r.json()

def need(uid, name):
    k = key_for(uid, name)
    if not k: raise HTTPException(412, f'Connect {CONNECTORS[name]["name"]} first: Settings, Connectors, paste your key.')
    return k

async def legal_cases(qy, kind, uid):
    k = key_for(uid, 'courtlistener'); h = {'Authorization': 'Token ' + k} if k else {}
    ver = 'v4' if k else 'v3'  # v4 search needs a token; v3 still answers without one, with lower limits
    r = await web_http.get(f'https://www.courtlistener.com/api/rest/{ver}/search/', params={'q': qy, 'type': 'o' if kind == 'cases' else 'r', 'order_by': 'score desc'}, headers=h, timeout=30)
    if r.status_code in (401, 403): raise HTTPException(412, 'CourtListener wants a free API token for this. Settings, Connectors, CourtListener.')
    if r.status_code >= 400: raise HTTPException(502, f'CourtListener answered {r.status_code}.')
    res = []
    for x in r.json().get('results', [])[:12]:
        if kind == 'cases':
            snip = x.get('snippet') or ((x.get('opinions') or [{}])[0].get('snippet') or '')
            res.append({'title': x.get('caseName'), 'court': x.get('court'), 'date': x.get('dateFiled'), 'citation': ', '.join(x.get('citation') or [])[:120],
                        'url': 'https://www.courtlistener.com' + (x.get('absolute_url') or ''), 'snippet': re.sub(r'<[^>]+>', '', snip)[:300]})
        else:
            res.append({'title': x.get('caseName'), 'court': x.get('court'), 'docket': x.get('docketNumber'), 'date': x.get('dateFiled'),
                        'url': 'https://www.courtlistener.com' + (x.get('docket_absolute_url') or x.get('absolute_url') or ''), 'parties': (x.get('party') or [])[:6]})
    return {'query': qy, 'results': res, 'sources': ['CourtListener (Free Law Project)' + (' with your token' if k else '')]}

async def run_tool(u, tool, target, b):
    t = target
    if tool in ('domain', 'ip', 'wayback', 'username', 'harvest'):
        return await osint_call('/' + tool, {'target': t}, timeout=190 if tool in HEAVY else 60), f'li-osint/{tool}'
    if tool in ('cases', 'dockets'):
        return await legal_cases(t, tool, u['id']), 'CourtListener API v4'
    if tool == 'fedreg':
        r = await web_http.get('https://www.federalregister.gov/api/v1/documents.json', params={'conditions[term]': t, 'per_page': 10, 'order': 'relevance'}, timeout=30)
        return {'query': t, 'results': [{'title': x.get('title'), 'type': x.get('type'), 'date': x.get('publication_date'), 'agencies': [a.get('name') for a in x.get('agencies') or []][:3],
                'url': x.get('html_url'), 'snippet': (x.get('abstract') or '')[:300]} for x in r.json().get('results', [])], 'sources': ['Federal Register API']}, 'Federal Register API'
    if tool == 'ecfr':
        r = await web_http.get('https://www.ecfr.gov/api/search/v1/results', params={'query': t, 'per_page': 10}, timeout=30)
        res = []
        for x in r.json().get('results', []):
            h = x.get('hierarchy') or {}
            path = f"title-{h.get('title')}/section-{h.get('section')}" if h.get('section') else f"title-{h.get('title')}/part-{h.get('part')}"
            res.append({'title': ' '.join(str(v) for v in (x.get('hierarchy_headings') or {}).values() if v)[:200] or x.get('headings', {}).get('section'),
                        'cite': f"{h.get('title')} CFR {h.get('section') or h.get('part')}", 'url': f'https://www.ecfr.gov/current/{path}', 'snippet': re.sub(r'<[^>]+>', '', x.get('full_text_excerpt') or '')[:300]})
        return {'query': t, 'results': res, 'sources': ['eCFR API']}, 'eCFR API'
    if tool == 'shodan':
        k = need(u['id'], 'shodan')
        try:
            if not ipaddress.ip_address(t).is_global: raise ValueError
        except ValueError:
            raise HTTPException(400, 'Shodan needs a public IP address.')
        r = await web_http.get(f'https://api.shodan.io/shodan/host/{t}', params={'key': k, 'minify': 'false'}, timeout=30)
        if r.status_code == 404: return {'ip': t, 'ports': [], 'note': 'Shodan has no record of this IP.'}, 'Shodan API'
        if r.status_code >= 400: raise HTTPException(502, f'Shodan answered {r.status_code}.')
        j = r.json()
        return {'ip': t, 'org': j.get('org'), 'isp': j.get('isp'), 'country': j.get('country_name'), 'hostnames': j.get('hostnames'), 'ports': j.get('ports'),
                'vulns': list(j.get('vulns') or [])[:30], 'last_update': j.get('last_update'),
                'services': [{'port': s.get('port'), 'product': s.get('product'), 'version': s.get('version'), 'banner': (s.get('data') or '')[:200]} for s in j.get('data', [])[:20]]}, 'Shodan API'
    if tool == 'hunter':
        k = need(u['id'], 'hunter')
        t = t.lower().replace('https://', '').replace('http://', '').split('/')[0]
        if not re.match(r'^[a-z0-9.-]+\.[a-z]{2,}$', t): raise HTTPException(400, 'Hunter.io needs a domain like example.com.')
        r = await web_http.get('https://api.hunter.io/v2/domain-search', params={'domain': t, 'api_key': k, 'limit': 50}, timeout=30)
        if r.status_code >= 400: raise HTTPException(502, f'Hunter.io answered {r.status_code}.')
        j = r.json().get('data', {})
        return {'domain': t, 'organization': j.get('organization'), 'pattern': j.get('pattern'),
                'emails': [{'value': e.get('value'), 'name': ' '.join(x for x in [e.get('first_name'), e.get('last_name')] if x), 'position': e.get('position'), 'confidence': e.get('confidence'),
                            'sources': [s.get('uri') for s in e.get('sources', [])[:3]]} for e in j.get('emails', [])]}, 'Hunter.io API'
    if tool == 'pwned':
        k = need(u['id'], 'hibp')
        if not re.match(r'^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$', t): raise HTTPException(400, 'Have I Been Pwned needs an email address.')
        r = await web_http.get(f'https://haveibeenpwned.com/api/v3/breachedaccount/{t}', params={'truncateResponse': 'false'}, headers={'hibp-api-key': k, 'user-agent': 'Li-case-tools'}, timeout=30)
        if r.status_code == 404: return {'email': t, 'breaches': []}, 'Have I Been Pwned API'
        if r.status_code >= 400: raise HTTPException(502, f'Have I Been Pwned answered {r.status_code}.')
        return {'email': t, 'breaches': [{'name': x.get('Name'), 'domain': x.get('Domain'), 'date': x.get('BreachDate'), 'data': x.get('DataClasses')} for x in r.json()]}, 'Have I Been Pwned API'
    if tool in ('scrape', 'fsearch', 'crawl'):
        k = need(u['id'], 'firecrawl'); H = {'Authorization': 'Bearer ' + k}
        if tool == 'scrape':
            public_url(t if t.startswith('http') else 'https://' + t)
            r = await web_http.post('https://api.firecrawl.dev/v2/scrape', json={'url': t, 'formats': ['markdown'], 'onlyMainContent': True}, headers=H, timeout=90)
            if r.status_code >= 400: raise HTTPException(502, f'Firecrawl answered {r.status_code}.')
            d = r.json().get('data', {}); return {'url': t, 'title': (d.get('metadata') or {}).get('title'), 'pages': 1, 'markdown': (d.get('markdown') or '')[:20000]}, 'Firecrawl scrape'
        if tool == 'fsearch':
            r = await web_http.post('https://api.firecrawl.dev/v2/search', json={'query': t, 'limit': 6}, headers=H, timeout=60)
            if r.status_code >= 400: raise HTTPException(502, f'Firecrawl answered {r.status_code}.')
            d = r.json().get('data', {}); web = d.get('web', d) if isinstance(d, dict) else d
            return {'query': t, 'results': [{'title': x.get('title'), 'url': x.get('url'), 'snippet': (x.get('description') or '')[:300]} for x in (web or [])][:6]}, 'Firecrawl search'
        public_url(t if t.startswith('http') else 'https://' + t)
        r = await web_http.post('https://api.firecrawl.dev/v2/crawl', json={'url': t, 'limit': 10, 'scrapeOptions': {'formats': ['markdown'], 'onlyMainContent': True}}, headers=H, timeout=60)
        if r.status_code >= 400: raise HTTPException(502, f'Firecrawl answered {r.status_code}.')
        jid = r.json().get('id')
        for _ in range(40):
            await asyncio.sleep(4)
            s = (await web_http.get(f'https://api.firecrawl.dev/v2/crawl/{jid}', headers=H, timeout=30)).json()
            if s.get('status') in ('completed', 'failed', 'cancelled'): break
        pages = [{'url': (p.get('metadata') or {}).get('sourceURL'), 'title': (p.get('metadata') or {}).get('title'), 'markdown': (p.get('markdown') or '')[:3000]} for p in s.get('data', [])[:10]]
        return {'url': t, 'status': s.get('status'), 'pages': len(pages), 'items': pages}, 'Firecrawl crawl'
    raise HTTPException(404, 'Unknown tool.')

TOOLS = {'domain', 'ip', 'wayback', 'username', 'harvest', 'cases', 'dockets', 'fedreg', 'ecfr', 'shodan', 'hunter', 'pwned', 'scrape', 'fsearch', 'crawl'}
@app.post('/li-auth/tool/{tool}')
async def tool(tool: str, req: Request):
    u = await who(req)
    if tool not in TOOLS: raise HTTPException(404, 'Unknown tool.')
    limit(req, 'tool' + u['id'], 40, 600)
    b = await req.json(); target = str(b.get('target') or '').strip()[:500]
    if not target: raise HTTPException(400, 'Give the tool something to look up.')
    if tool in HEAVY:
        limit(req, 'heavy' + u['id'], 8, 600)
        async with _heavy:
            data, source = await run_tool(u, tool, target, b)
    else:
        data, source = await asyncio.wait_for(run_tool(u, tool, target, b), 120)
    ev = save_evidence(u, case_of(b), tool, target, data, source)
    return {'tool': tool, 'target': target, 'evidence': ev, 'data': data}

@app.post('/li-auth/tool-exif')
async def tool_exif(req: Request):
    u = await who(req); limit(req, 'tool' + u['id'], 40, 600)
    body = await req.body()
    if not body or len(body) > 30_000_000: raise HTTPException(400, 'Send one photo up to 30 MB.')
    fsha = hashlib.sha256(body).hexdigest()
    try:
        r = await http.post(OSINT + '/exif', content=body, timeout=40)
    except Exception:
        raise HTTPException(502, 'The OSINT tools are not answering right now.')
    if r.status_code >= 400: raise HTTPException(502, 'Could not read that photo.')
    data = r.json(); name = req.headers.get('x-file-name', 'photo')[:120]
    case = (req.headers.get('x-case') or 'General')[:80]
    ev = save_evidence(u, case, 'exif', name, data, 'exiftool', file_sha=fsha)
    return {'tool': 'exif', 'target': name, 'evidence': ev, 'data': data}

def _like(s): return '%' + s.replace('%', '').replace('_', '') + '%'
@app.get('/li-auth/case/{case}/evidence')
async def case_evidence(case: str, req: Request, q_: str = '', tag: str = ''):
    u = await who(req); qs = req.query_params.get('q', '')
    sql, args = 'select id,kind,target,summary,sha256,file_sha256,source,tags,created_at from evidence where user_id=? and case_name=?', [u['id'], case]
    if qs: sql += ' and (target like ? or summary like ? or tags like ? or data like ?)'; args += [_like(qs)] * 4
    return [dict(r) for r in q(sql + ' order by created_at desc limit 300', args)]

@app.get('/li-auth/evidence/{eid}')
async def evidence_one(eid: int, req: Request):
    u = await who(req); r = q('select * from evidence where id=? and user_id=?', (eid, u['id']), one=True)
    if not r: raise HTTPException(404, 'Not found.')
    d = dict(r); d['verified'] = hashlib.sha256(d['data'].encode()).hexdigest() == d['sha256']; d['data'] = json.loads(d['data']); return d

@app.get('/li-auth/case/{case}/entities')
async def case_entities(case: str, req: Request):
    u = await who(req); qs = req.query_params.get('q', '')
    sql, args = 'select * from entities where user_id=? and case_name=?', [u['id'], case]
    if qs: sql += ' and (value like ? or note like ? or tags like ? or etype like ?)'; args += [_like(qs)] * 4
    return [dict(r) for r in q(sql + ' order by etype, last_seen desc limit 600', args)]

@app.get('/li-auth/case/{case}/timeline')
async def case_timeline(case: str, req: Request):
    u = await who(req)
    ev = [{'at': r['created_at'], 'what': f"{r['kind']}: {r['target']}", 'detail': r['summary'], 'ref': f"E{r['id']}", 'tags': r['tags']} for r in q('select * from evidence where user_id=? and case_name=?', (u['id'], case))]
    nt = [{'at': r['at'], 'what': 'Note', 'detail': r['text'], 'ref': f"N{r['id']}", 'tags': ''} for r in q('select * from notes where user_id=? and case_name=?', (u['id'], case))]
    return sorted(ev + nt, key=lambda x: x['at'], reverse=True)[:500]

@app.post('/li-auth/case/{case}/note')
async def case_note(case: str, req: Request):
    u = await who(req); b = await req.json(); txt = str(b.get('text') or '').strip()[:2000]
    if not txt: raise HTTPException(400, 'Write something.')
    at = int(b.get('at') or now())
    q('insert into notes(user_id,case_name,text,at,created_at) values(?,?,?,?,?)', (u['id'], case[:80], txt, at, now())); return {'ok': True}

@app.post('/li-auth/tag/{kind}/{iid}')
async def set_tags(kind: str, iid: int, req: Request):
    u = await who(req); b = await req.json()
    tags = ','.join(sorted({t.strip().lower()[:30] for t in str(b.get('tags') or '').split(',') if t.strip()}))[:300]
    if kind == 'evidence': q('update evidence set tags=? where id=? and user_id=?', (tags, iid, u['id']))
    elif kind == 'entity':
        q('update entities set tags=? where id=? and user_id=?', (tags, iid, u['id']))
        if 'note' in b: q('update entities set note=? where id=? and user_id=?', (str(b['note'])[:1000], iid, u['id']))
    else: raise HTTPException(404, 'Unknown.')
    return {'ok': True, 'tags': tags}

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
