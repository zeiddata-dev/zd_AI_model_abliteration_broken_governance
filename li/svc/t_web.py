import sys, asyncio
sys.argv = ['t']; sys.path.insert(0, '/app/backend/data/li')
import li_auth as L
async def main():
    r = await L.http.get(L.SEARX + '/search', params={'q': 'fentanyl erowid', 'format': 'json', 'language': 'en'})
    j = r.json(); print('SEARCH', r.status_code, len(j.get('results', [])), [x['url'] for x in j.get('results', [])[:3]])
    print('ENGINES_UNRESPONSIVE', j.get('unresponsive_engines', [])[:5])
    for u in ['https://erowid.org/', 'https://osintframework.com/', 'https://en.wikipedia.org/wiki/Cheque_fraud']:
        try:
            L.public_url(u); rr = await L.web_http.get(u)
            while rr.is_redirect: rr = await L.web_http.get(L.urljoin(u, rr.headers['location']))
            t, txt = L.page_text(rr.text); print('READ', rr.status_code, u, '|', t[:60], '|', len(txt), 'chars |', txt[:120].replace('\n', ' '))
        except Exception as e: print('READ_FAIL', u, e)
    for bad in ['http://127.0.0.1:3000/', 'http://169.254.169.254/latest/meta-data/', 'http://localhost:8888/', 'file:///etc/passwd', 'http://10.0.0.5/']:
        try: L.public_url(bad); print('NOT_BLOCKED', bad)
        except Exception as e: print('BLOCKED', bad)
    try:
        import bs4; print('BS4', bs4.__version__)
    except Exception as e: print('NO_BS4')
asyncio.run(main())
