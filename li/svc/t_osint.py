"""Self-test on the box, same code path as Li minus the sign-in. Uses harmless targets and a throwaway case."""
import sys, asyncio, json
sys.argv = ['t']; sys.path.insert(0, '/app/backend/data/li')
import li_auth as L
U = {'id': 'selftest-no-user'}
async def one(tool, target):
    try:
        d, src = await L.run_tool(U, tool, target, {})
        ev = L.save_evidence(U, 'selftest', tool, target, d, src)
        print('OK  ', tool, target, '|', ev['summary'][:150], '| sha', ev['sha256'][:12])
    except Exception as e:
        print('FAIL', tool, target, '|', getattr(e, 'detail', e))
async def main():
    for t, x in [('domain', 'example.com'), ('ip', '1.1.1.1'), ('wayback', 'example.com'), ('harvest', 'example.com'), ('username', 'nasa'),
                 ('cases', 'check kiting'), ('dockets', 'check fraud'), ('fedreg', 'money transmitter'), ('ecfr', 'suspicious activity report')]:
        await one(t, x)
    for t, x in [('shodan', '1.1.1.1'), ('scrape', 'https://example.com')]:
        await one(t, x)
    n = L.q('select count(*) n from entities where user_id=?', (U['id'],), one=True)['n']
    v = L.q('select data, sha256 from evidence where user_id=? limit 1', (U['id'],), one=True)
    print('ENTITIES', n, '| HASH_VERIFIES', L.hashlib.sha256(v['data'].encode()).hexdigest() == v['sha256'])
    print('ENCRYPTION_ROUNDTRIP', L.FERNET.decrypt(L.FERNET.encrypt(b'abc123xyz')) == b'abc123xyz')
    L.q('delete from evidence where user_id=?', (U['id'],)); L.q('delete from entities where user_id=?', (U['id'],))
asyncio.run(main())
