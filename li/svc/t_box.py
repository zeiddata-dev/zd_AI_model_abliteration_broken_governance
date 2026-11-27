"""End-to-end test of li-auth against a throwaway Open WebUI, with a software passkey."""
import base64, cbor2, hashlib, json, os, struct, sys, httpx
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes
B = sys.argv[1]; ORIGIN = sys.argv[2]; RP = sys.argv[3]; ADM = (sys.argv[4], sys.argv[5])
c = httpx.Client(base_url=B, timeout=60, headers={'Origin': ORIGIN})
b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b'=').decode()
ub = lambda s: base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))
ok = lambda name, cond, extra='': print(('PASS ' if cond else 'FAIL ') + name, extra)
H = lambda t: {'Authorization': 'Bearer ' + t}
r = c.post('/api/v1/auths/signin', json={'email': ADM[0], 'password': ADM[1]}); A = r.json()['token']; ok('admin signin', r.status_code == 200)
ok('invites need admin', c.get('/li-auth/invites').status_code == 401)
ok('tts needs sign-in', c.post('/li-auth/tts', json={'text': 'hi'}).status_code == 401)
r = c.post('/li-auth/invites', headers=H(A), json={'label': 'Sam', 'email': 'sam@li.local', 'days': 7}); inv = r.json(); ok('make invite', r.status_code == 200 and '-' in inv['code'], inv.get('link'))
ok('check invite', c.post('/li-auth/invite/check', json={'code': inv['code'].lower()}).json().get('email') == 'sam@li.local')
ok('wrong email refused', c.post('/li-auth/invite/redeem', json={'code': inv['code'], 'name': 'X', 'email': 'x@li.local', 'password': 'longpassword1'}).status_code == 400)
r = c.post('/li-auth/invite/redeem', json={'code': inv['code'], 'name': 'Sam Test', 'email': 'sam@li.local', 'password': 'longpassword1'}); j = r.json(); ok('redeem', r.status_code == 200 and j.get('token'), str(j)[:120])
S = j['token']; sid = j['id']
ok('invite single use', c.post('/li-auth/invite/redeem', json={'code': inv['code'], 'name': 'Sam', 'email': 'sam@li.local', 'password': 'longpassword1'}).status_code == 404)
me = c.get('/api/v1/auths/', headers=H(S)).json(); ok('new user active', me.get('role') == 'user', me.get('role'))
ok('user cannot list invites', c.get('/li-auth/invites', headers=H(S)).status_code == 403)
ok('signup still closed', c.post('/api/v1/auths/signup', json={'name': 'z', 'email': 'z@li.local', 'password': 'longpassword1'}).status_code in (403, 401))
# passkey register
key = ec.generate_private_key(ec.SECP256R1()); n = key.public_key().public_numbers()
cose = {1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, 'big'), -3: n.y.to_bytes(32, 'big')}; cid = os.urandom(16)
rph = hashlib.sha256(RP.encode()).digest()
o = c.post('/li-auth/passkey/register/options', headers=H(S), json={}).json()
ad = rph + bytes([0x45]) + struct.pack('>I', 0) + b'\0' * 16 + struct.pack('>H', len(cid)) + cid + cbor2.dumps(cose)
cdj = json.dumps({'type': 'webauthn.create', 'challenge': o['challenge'], 'origin': ORIGIN, 'crossOrigin': False}).encode()
cred = {'id': b64u(cid), 'rawId': b64u(cid), 'type': 'public-key', 'response': {'clientDataJSON': b64u(cdj), 'attestationObject': b64u(cbor2.dumps({'fmt': 'none', 'attStmt': {}, 'authData': ad})), 'transports': ['internal']}, 'clientExtensionResults': {}}
r = c.post('/li-auth/passkey/register/verify', headers=H(S), json={'credential': cred, 'name': 'iPhone'}); ok('passkey register', r.status_code == 200, r.text[:120])
ok('passkey listed', len(c.get('/li-auth/passkeys', headers=H(S)).json()) == 1)
def plogin(count):
    lo = c.post('/li-auth/passkey/login/options', json={}).json()
    ad2 = rph + bytes([0x05]) + struct.pack('>I', count)
    cd2 = json.dumps({'type': 'webauthn.get', 'challenge': lo['options']['challenge'], 'origin': ORIGIN, 'crossOrigin': False}).encode()
    sig = key.sign(ad2 + hashlib.sha256(cd2).digest(), ec.ECDSA(hashes.SHA256()))
    return c.post('/li-auth/passkey/login/verify', json={'challenge_id': lo['challenge_id'], 'credential': {'id': b64u(cid), 'rawId': b64u(cid), 'type': 'public-key',
        'response': {'clientDataJSON': b64u(cd2), 'authenticatorData': b64u(ad2), 'signature': b64u(sig), 'userHandle': o['user']['id']}, 'clientExtensionResults': {}}})
r = plogin(1); P = r.json().get('token'); ok('passkey login', r.status_code == 200 and P, r.text[:120])
me = c.get('/api/v1/auths/', headers=H(P)).json(); ok('passkey token works in Open WebUI', me.get('email') == 'sam@li.local')
ok('passkey replay refused', plogin(1).status_code == 400)
r = c.post('/li-auth/tts', headers=H(P), json={'text': 'Hey, it is Li. Your box is warm and ready.', 'voice': 'af_heart'}); ok('tts', r.status_code == 200 and r.content[:4] == b'RIFF', f'{len(r.content)} bytes')
us = c.get('/api/v1/users/?page=1', headers=H(A)).json(); ok('users list shape', 'users' in us, str(list(us.keys()) if isinstance(us, dict) else type(us)))
u = [x for x in us['users'] if x['id'] == sid][0]
r = c.post(f'/api/v1/users/{sid}/update', headers=H(A), json={'role': 'pending', 'name': u['name'], 'email': u['email'], 'profile_image_url': u.get('profile_image_url') or '/user.png'}); ok('pause user', r.status_code == 200, r.text[:160])
ok('paused passkey refused', plogin(2).status_code == 403)
c.post(f'/api/v1/users/{sid}/update', headers=H(A), json={'role': 'user', 'name': u['name'], 'email': u['email'], 'profile_image_url': u.get('profile_image_url') or '/user.png'})
ok('approved again', plogin(3).status_code == 200)
i2 = c.post('/li-auth/invites', headers=H(A), json={'label': 'Revoke me'}).json(); c.delete('/li-auth/invites/' + i2['id'], headers=H(A))
ok('cancelled invite refused', c.post('/li-auth/invite/check', json={'code': i2['code']}).status_code == 404)
st = {x['label']: x['status'] for x in c.get('/li-auth/invites', headers=H(A)).json()}; ok('invite statuses', st == {'Sam': 'used', 'Revoke me': 'revoked'}, str(st))
ok('remove user', c.delete(f'/api/v1/users/{sid}', headers=H(A)).status_code == 200)
ok('removed user passkey refused', plogin(4).status_code == 404)
