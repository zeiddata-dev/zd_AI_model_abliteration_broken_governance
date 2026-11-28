"""li-osint: passive, public-source OSINT tools for Li, on 127.0.0.1 only, run sandboxed by systemd.
Only li-auth (which checks the sign-in and rate limits) calls it. No active scanning: lookups read
public registries, DNS, certificate logs, archives and public profile pages."""
import asyncio, ipaddress, json, os, re, shutil, socket, subprocess, tempfile, time
from concurrent.futures import ThreadPoolExecutor
import httpx
from fastapi import FastAPI, HTTPException, Request

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36'
http = httpx.AsyncClient(timeout=25, follow_redirects=True, headers={'User-Agent': UA})
pool = ThreadPoolExecutor(8)
DOM = re.compile(r'^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$')
USER = re.compile(r'^[A-Za-z0-9._-]{2,40}$')
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')

def domain(v):
    d = str(v or '').strip().lower()
    d = re.sub(r'^https?://', '', d).split('/')[0].split(':')[0].strip('.')
    if d.startswith('www.'): d = d[4:]
    if not DOM.match(d): raise HTTPException(400, 'That is not a domain name.')
    return d

def public_ip(v):
    try: ip = ipaddress.ip_address(str(v).strip())
    except ValueError: raise HTTPException(400, 'That is not an IP address.')
    if not ip.is_global: raise HTTPException(400, 'Only public IP addresses.')
    return str(ip)

async def run(fn, *a, timeout=20):
    return await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(pool, fn, *a), timeout)

def _whois(d):
    import whois
    w = whois.whois(d)
    keep = ['domain_name', 'registrar', 'whois_server', 'creation_date', 'updated_date', 'expiration_date', 'name_servers', 'status',
            'emails', 'name', 'org', 'address', 'city', 'state', 'country', 'registrant_postal_code', 'dnssec']
    out = {}
    for k in keep:
        v = w.get(k) if hasattr(w, 'get') else None
        if v in (None, '', []): continue
        if isinstance(v, list): v = sorted({str(x) for x in v})[:12]
        else: v = str(v)
        out[k] = v
    return out

def _dns(d):
    import dns.resolver
    r = dns.resolver.Resolver(); r.lifetime = 5; r.timeout = 4
    out = {}
    for t in ['A', 'AAAA', 'MX', 'NS', 'TXT', 'CNAME', 'SOA', 'CAA']:
        try: out[t] = sorted({x.to_text().strip('"') for x in r.resolve(d, t)})[:20]
        except Exception: pass
    return out

def _ptr(ip):
    try: return socket.gethostbyaddr(ip)[0]
    except Exception: return None

async def crtsh(d):
    try:
        r = await http.get('https://crt.sh/', params={'q': '%.' + d, 'output': 'json'}, timeout=30)
        rows = r.json()
    except Exception as e:
        return {'error': 'crt.sh did not answer'}
    names, first, last = set(), None, None
    for x in rows:
        for n in str(x.get('name_value', '')).split('\n'):
            n = n.strip().lower().lstrip('*.')
            if n.endswith(d): names.add(n)
        nb = x.get('not_before')
        if nb: first = min(first or nb, nb); last = max(last or nb, nb)
    return {'certificates': len(rows), 'names': sorted(names)[:300], 'first_seen': first, 'last_seen': last}

async def wayback(target, exact=False):
    base = {'url': target, 'output': 'json', 'fl': 'timestamp,original,statuscode', 'collapse': 'timestamp:4', 'limit': '200'}
    # Whole-host searches time out on big sites, so fall back to the exact address.
    tries = [('exact address', base, 35)] if exact else [('whole site', {**base, 'matchType': 'host'}, 15), ('home page only', base, 35)]
    rows, scope = None, None
    for label, p, tmo in tries:
        try:
            r = await http.get('https://web.archive.org/cdx/search/cdx', params=p, timeout=tmo)
            if r.status_code != 200: continue
            rows, scope = (r.json()[1:] if r.text.strip() else []), label
            break
        except Exception:
            continue
    if rows is None:
        return {'error': 'Wayback Machine did not answer'}
    years = {}
    for ts, orig, st in rows: years[ts[:4]] = years.get(ts[:4], 0) + 1
    snaps = [{'when': ts, 'url': f'https://web.archive.org/web/{ts}/{orig}'} for ts, orig, st in rows]
    return {'scope': scope, 'yearly_captures': len(rows), 'first': snaps[0] if snaps else None, 'last': snaps[-1] if snaps else None,
            'by_year': years, 'snapshots': snaps[-12:]}

@app.get('/health')
async def health():
    return {'ok': True, 'exiftool': bool(shutil.which('exiftool')), 'maigret': bool(shutil.which('maigret') or os.path.exists(os.path.join(os.path.dirname(os.sys.executable), 'maigret')))}

@app.post('/domain')
async def domain_lookup(req: Request):
    d = domain((await req.json()).get('target'))
    async def safe(coro, name):
        try: return await coro
        except Exception as e: return {'error': f'{name} lookup failed'}
    w, dn, c, wb = await asyncio.gather(safe(run(_whois, d, timeout=25), 'WHOIS'), safe(run(_dns, d), 'DNS'), safe(crtsh(d), 'crt.sh'), safe(wayback(d), 'Wayback'))
    return {'domain': d, 'whois': w, 'dns': dn, 'crtsh': c, 'wayback': wb,
            'sources': ['WHOIS (port 43 registry servers)', 'DNS (public resolvers)', 'crt.sh certificate transparency', 'Internet Archive CDX']}

@app.post('/ip')
async def ip_lookup(req: Request):
    ip = public_ip((await req.json()).get('target'))
    try:
        r = await http.get(f'https://rdap.org/ip/{ip}', timeout=25); rd = r.json()
    except Exception:
        rd = {'error': 'RDAP did not answer'}
    keep = {k: rd.get(k) for k in ['handle', 'name', 'type', 'country', 'startAddress', 'endAddress', 'parentHandle'] if rd.get(k)}
    ents = []
    for e in rd.get('entities', []) if isinstance(rd, dict) else []:
        v = e.get('vcardArray', [None, []])[1] if e.get('vcardArray') else []
        fn = next((x[3] for x in v if x and x[0] == 'fn'), None)
        em = next((x[3] for x in v if x and x[0] == 'email'), None)
        ents.append({'roles': e.get('roles'), 'name': fn, 'email': em, 'handle': e.get('handle')})
    return {'ip': ip, 'reverse_dns': await run(_ptr, ip, timeout=8), 'rdap': keep, 'contacts': ents[:10], 'sources': ['RDAP (rdap.org bootstrap to the regional registry)', 'Reverse DNS']}

@app.post('/wayback')
async def wayback_lookup(req: Request):
    t = str((await req.json()).get('target') or '').strip()
    if not re.match(r'^(https?://)?[A-Za-z0-9.-]+\.[A-Za-z]{2,}(/\S*)?$', t): raise HTTPException(400, 'Give a web address or domain.')
    exact = '/' in re.sub(r'^https?://', '', t).rstrip('/')
    return {'target': t, **(await wayback(re.sub(r'^https?://', '', t), exact=exact)), 'sources': ['Internet Archive CDX']}

@app.post('/username')
async def username_sweep(req: Request):
    u = str((await req.json()).get('target') or '').strip().lstrip('@')
    if not USER.match(u): raise HTTPException(400, 'Usernames are 2 to 40 letters, numbers, dots, dashes or underscores.')
    exe = shutil.which('maigret') or os.path.join(os.path.dirname(os.sys.executable), 'maigret')
    out = tempfile.mkdtemp(prefix='mg_')
    cmd = [exe, u, '--top-sites', '500', '--timeout', '8', '--retries', '0', '-n', '60', '--no-recursion', '--no-progressbar', '--no-color', '-J', 'simple', '-fo', out]
    t0 = time.time()
    try:
        p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try: await asyncio.wait_for(p.wait(), 170)
        except asyncio.TimeoutError: p.kill(); raise HTTPException(504, 'The username sweep ran out of time.')
        rep = next((os.path.join(out, f) for f in os.listdir(out) if f.endswith('.json')), None)
        data = json.load(open(rep)) if rep else {}
    finally:
        shutil.rmtree(out, ignore_errors=True)
    found = []
    for site, v in (data or {}).items():
        st = v.get('status') if isinstance(v, dict) else None
        status = (st or {}).get('status') if isinstance(st, dict) else st
        if str(status).lower() == 'claimed':
            found.append({'site': site, 'url': v.get('url_user') or (st or {}).get('url'), 'tags': (st or {}).get('tags', [])[:4] if isinstance(st, dict) else []})
    found.sort(key=lambda x: x['site'].lower())
    return {'username': u, 'checked_sites': 500, 'found': found, 'seconds': round(time.time() - t0),
            'note': 'A match means a public profile page answered for this name. It is not proof the same person owns every account.',
            'sources': ['Maigret, top 500 sites, public profile pages only']}

@app.post('/harvest')
async def harvest(req: Request):
    d = domain((await req.json()).get('target'))
    subs, emails, src = set(), set(), {}
    async def crt():
        x = await crtsh(d); n = x.get('names', []); subs.update(n); src['crt.sh'] = len(n) if 'error' not in x else 'no answer'
    async def otx():
        try:
            r = await http.get(f'https://otx.alienvault.com/api/v1/indicators/domain/{d}/passive_dns', timeout=25)
            n = {x['hostname'].lower() for x in r.json().get('passive_dns', []) if str(x.get('hostname', '')).lower().endswith(d)}
            subs.update(n); src['AlienVault OTX passive DNS'] = len(n)
        except Exception: src['AlienVault OTX passive DNS'] = 'no answer'
    async def ht():
        try:
            r = await http.get('https://api.hackertarget.com/hostsearch/', params={'q': d}, timeout=25)
            n = {l.split(',')[0].lower() for l in r.text.splitlines() if ',' in l and l.split(',')[0].lower().endswith(d)}
            subs.update(n); src['HackerTarget hostsearch'] = len(n) if n or 'error' not in r.text.lower() else r.text.strip()[:60]
        except Exception: src['HackerTarget hostsearch'] = 'no answer'
    async def us():
        try:
            r = await http.get('https://urlscan.io/api/v1/search/', params={'q': f'domain:{d}', 'size': 100}, timeout=25)
            n = {x['page']['domain'].lower() for x in r.json().get('results', []) if str(x.get('page', {}).get('domain', '')).lower().endswith(d)}
            subs.update(n); src['urlscan.io'] = len(n)
        except Exception: src['urlscan.io'] = 'no answer'
    async def site_emails():
        got = 0
        for path in ['', 'contact', 'about', 'contact-us', 'privacy']:
            try:
                r = await http.get(f'https://{d}/{path}', timeout=12)
                if r.status_code < 400:
                    for e in EMAIL.findall(r.text):
                        e = e.lower().strip('.')
                        if e.endswith(d) and not re.search(r'\.(png|jpg|gif|svg|webp)$', e): emails.add(e); got += 1
            except Exception: pass
        src['Emails on the site\'s own public pages'] = len(emails)
    await asyncio.gather(crt(), otx(), ht(), us(), site_emails())
    subs.discard(d)
    return {'domain': d, 'subdomains': sorted(subs)[:400], 'emails': sorted(emails)[:100], 'sources': src,
            'note': 'Passive sources only: certificate logs, passive DNS, public scan archives and the site\'s own pages.'}

@app.post('/exif')
async def exif(req: Request):
    body = await req.body()
    if not body or len(body) > 30_000_000: raise HTTPException(400, 'Send one photo up to 30 MB.')
    if not shutil.which('exiftool'): raise HTTPException(503, 'exiftool is not installed.')
    fd, path = tempfile.mkstemp(prefix='ex_')
    try:
        os.write(fd, body); os.close(fd)
        p = await asyncio.create_subprocess_exec('exiftool', '-j', '-G1', '-a', '-n', '-b', '-x', 'ThumbnailImage', '-x', 'PreviewImage', '-x', 'JpgFromRaw', path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), 25)
        tags = (json.loads(out or b'[{}]') or [{}])[0]
    finally:
        try: os.remove(path)
        except Exception: pass
    tags = {k: v for k, v in tags.items() if not k.startswith('System:') and k not in ('SourceFile',) and not (isinstance(v, str) and v.startswith('base64:'))}
    gps = None
    def coord(name):
        # Composite tags are signed; GPS-group tags are unsigned and need their N/S/E/W ref.
        v = tags.get('Composite:' + name)
        if isinstance(v, (int, float)): return v
        v = next((x for k, x in tags.items() if k.endswith(':' + name)), None)
        if not isinstance(v, (int, float)): return None
        ref = str(next((x for k, x in tags.items() if k.endswith(':' + name + 'Ref')), '')).upper()
        return -abs(v) if ref.startswith(('S', 'W')) else v
    lat, lon = coord('GPSLatitude'), coord('GPSLongitude')
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)): gps = {'lat': lat, 'lon': lon, 'map': f'https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=17/{lat}/{lon}'}
    pick = lambda *names: next((tags[k] for k in tags for n in names if k.endswith(':' + n)), None)
    key = {'camera': ' '.join(str(x) for x in [pick('Make'), pick('Model')] if x) or None, 'taken': pick('DateTimeOriginal', 'CreateDate'),
           'software': pick('Software'), 'lens': pick('LensModel'), 'size': f"{pick('ImageWidth')}x{pick('ImageHeight')}" if pick('ImageWidth') else None}
    return {'summary': {k: v for k, v in key.items() if v}, 'gps': gps, 'tags': dict(list(tags.items())[:250]), 'sources': ['exiftool']}

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=int(os.environ.get('LI_OSINT_PORT', '3014')), log_level='warning')
