"""Stop every running li-auth server inside the open-webui container, whatever its pid file says.
Matches processes whose command line runs /app/backend/data/li/li_auth.py, so self-tests that
import li_auth are left alone. Waits until port 3010 is free."""
import os, signal, socket, time
TARGET = '/app/backend/data/li/li_auth.py'
PORT = int(os.environ.get('LI_PORT', '3010'))
def servers():
    out = []
    for d in os.listdir('/proc'):
        if not d.isdigit() or int(d) == os.getpid(): continue
        try: argv = open(f'/proc/{d}/cmdline', 'rb').read().split(b'\0')
        except OSError: continue
        if TARGET.encode() in argv: out.append(int(d))
    return out
def port_busy():
    s = socket.socket()
    try: return s.connect_ex(('127.0.0.1', PORT)) == 0
    finally: s.close()
for sig, wait in ((signal.SIGTERM, 6), (signal.SIGKILL, 3)):
    for pid in servers():
        try: os.kill(pid, sig)
        except ProcessLookupError: pass
    end = time.time() + wait
    while time.time() < end and (servers() or port_busy()): time.sleep(0.2)
    if not servers() and not port_busy(): break
print('li_auth_stopped', 'port_free' if not port_busy() else 'PORT_STILL_BUSY')
