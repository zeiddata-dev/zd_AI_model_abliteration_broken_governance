#!/usr/bin/env bash
# setup_li_osint.sh
# OSINT tools for Li: li-osint service (WHOIS, DNS, crt.sh, Wayback, RDAP, exiftool, Maigret username sweep, passive domain harvest)
# on 127.0.0.1:3014 only, as a locked-down systemd service (throwaway user, read-only system, no home, private /tmp,
# no private-network egress). Updates li-auth (tools, case workspace, connectors) and li-tts.
# Usage: bash setup_li_osint.sh [git ref]    Rollback: sudo systemctl disable --now li-osint
REF=${1:-claude/qwen27b-openwebui}
RAW=https://raw.githubusercontent.com/zeiddata-dev/zd_ai_model_abliteration_broken_governance/$REF
TS=$(date -u +%Y%m%dT%H%M%SZ); LOG=/tmp/li_osint_${TS}.log; PTR=/tmp/li_osint_last.env
exec > >(tee -a "$LOG") 2>&1
ok(){ echo "PASS  $1"; }; bad(){ echo "FAIL  $1"; echo "DECISION=$2" | tee "$PTR"; exit 1; }
W=$(mktemp -d); cd "$W" || exit 1
for f in li/svc/li_osint.py li/svc/li_auth.py li/svc/li_auth_stop.py li/svc/t_osint.py; do curl -fsSL --max-time 30 "$RAW/$f" -o "$(basename $f)" || bad "download $f" ABORT_DOWNLOAD; done
command -v exiftool >/dev/null || { sudo DEBIAN_FRONTEND=noninteractive apt-get -qq update && sudo DEBIAN_FRONTEND=noninteractive apt-get -qq install -y libimage-exiftool-perl >/dev/null; } || bad "exiftool" ABORT_EXIFTOOL
sudo mkdir -p /opt/li-osint
[ -x /opt/li-osint/venv/bin/maigret ] || { sudo python3 -m venv /opt/li-osint/venv && sudo /opt/li-osint/venv/bin/pip -q install fastapi uvicorn httpx python-whois==0.9.6 dnspython maigret==0.6.7; } || bad "osint packages" ABORT_PIP
sudo install -m 644 li_osint.py /opt/li-osint/li_osint.py
sudo tee /etc/systemd/system/li-osint.service >/dev/null <<U
[Unit]
Description=Li OSINT tools (passive lookups, local only)
After=network-online.target
[Service]
Environment=LI_OSINT_PORT=3014 HOME=/tmp PATH=/opt/li-osint/venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/opt/li-osint/venv/bin/python /opt/li-osint/li_osint.py
DynamicUser=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
NoNewPrivileges=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
IPAddressDeny=10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10 fc00::/7 fe80::/10
CapabilityBoundingSet=
MemoryMax=1500M
CPUQuota=200%
TasksMax=300
Nice=5
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
U
sudo systemctl daemon-reload; sudo systemctl enable -q li-osint; sudo systemctl restart li-osint
for i in $(seq 45); do curl -sf 127.0.0.1:3014/health >/dev/null && break; sleep 2; done
H=$(curl -s 127.0.0.1:3014/health); echo "osint_health=$H"; echo "$H" | grep -q '"ok":true' && ok "li-osint on 127.0.0.1:3014" || { sudo journalctl -u li-osint -n 30 --no-pager; bad "li-osint did not start" ABORT_OSINT; }
B=$(sudo ss -ltn | awk '{print $4}' | grep ':3014$'); [ "$B" = "127.0.0.1:3014" ] && ok "3014 bound to 127.0.0.1 only" || bad "3014 listening beyond localhost: $B" ABORT_BIND
echo "sandbox: $(systemctl show li-osint -p DynamicUser -p ProtectSystem -p PrivateTmp -p NoNewPrivileges | tr '\n' ' ')"
# metadata endpoint must be unreachable from the sandbox
sudo systemd-run -q --wait --pipe -p DynamicUser=yes -p IPAddressDeny=169.254.0.0/16 curl -s -m 4 -o /dev/null -w 'metadata_from_sandbox=%{http_code}\n' http://169.254.169.254/latest/meta-data/ 2>/dev/null || echo "metadata_from_sandbox=blocked"
sudo docker cp li_auth.py open-webui:/app/backend/data/li/li_auth.py || bad "copy li_auth" ABORT_COPY
sudo docker cp li_auth_stop.py open-webui:/app/backend/data/li/li_auth_stop.py || bad "copy li_auth_stop" ABORT_COPY
# stop li-auth by its command line, not a pid file (a stale pid file left an old copy holding port 3010)
sudo mkdir -p /etc/systemd/system/li-auth.service.d
sudo tee /etc/systemd/system/li-auth.service.d/stop.conf >/dev/null <<'D'
[Service]
ExecStartPre=
ExecStartPre=-/usr/bin/docker exec open-webui python /app/backend/data/li/li_auth_stop.py
ExecStop=
ExecStop=-/usr/bin/docker exec open-webui python /app/backend/data/li/li_auth_stop.py
D
sudo systemctl daemon-reload
sudo systemctl stop li-auth
sudo docker exec open-webui python /app/backend/data/li/li_auth_stop.py
sudo systemctl start li-auth
for i in $(seq 60); do curl -sf 127.0.0.1:3010/li-auth/health >/dev/null && break; sleep 2; done
curl -sf 127.0.0.1:3010/li-auth/health >/dev/null && ok "li-auth back up" || { sudo journalctl -u li-auth -n 30 --no-pager; bad "li-auth did not come back" ABORT_AUTH; }
sleep 8; R0=$(systemctl show li-auth -p NRestarts --value)
NEWR=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{"target":"example.com"}' 127.0.0.1:3010/li-auth/tool/domain)
[ "$NEWR" = "401" ] && ok "li-auth is the new code (tool route asks for sign-in)" || { sudo journalctl -u li-auth -n 30 --no-pager; bad "li-auth tool route answered $NEWR, expected 401" ABORT_AUTH_OLD; }
sleep 12; [ "$(systemctl show li-auth -p NRestarts --value)" = "$R0" ] && ok "li-auth stable (no restarts)" || bad "li-auth is restarting" ABORT_AUTH_LOOP
# exif test photo with harmless tags
echo '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA0JCgsKCA0LCgsODg0PEyAVExISEyccHhcgLikxMC4pLSwzOko+MzZGNywtQFdBRkxOUlNSMj5aYVpQYEpRUk//2wBDAQ4ODhMREyYVFSZPNS01T09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT0//wAARCAAQABADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwCrRRRXkH0h/9k=' | base64 -d > /tmp/li_exif_test.jpg
exiftool -q -overwrite_original -Make=TestCam -Model=Li-Selftest -DateTimeOriginal='2026:11:27 12:00:00' -GPSLatitude=32.7767 -GPSLatitudeRef=N -GPSLongitude=96.7970 -GPSLongitudeRef=W /tmp/li_exif_test.jpg
echo "exif_test=$(curl -s -X POST --data-binary @/tmp/li_exif_test.jpg 127.0.0.1:3014/exif | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("summary"),d.get("gps"))')"
rm -f /tmp/li_exif_test.jpg
sudo docker cp t_osint.py open-webui:/tmp/t_osint.py
sudo docker exec open-webui python /tmp/t_osint.py > /tmp/t_osint.out 2>&1; echo "t_osint_rc=$?"
grep -E "^(OK|FAIL|ENTITIES|ENCRYPTION)" /tmp/t_osint.out || tail -n 25 /tmp/t_osint.out
for p in tool/domain tool/username connectors case/OSINT/evidence; do printf '%s_no_signin=%s\n' "$p" "$(curl -s -o /dev/null -w '%{http_code}' -X $( [ "$p" = connectors ] || [ "${p#case}" != "$p" ] && echo GET || echo POST) -H 'Content-Type: application/json' -d '{"target":"example.com"}' https://chat.zeiddata.com/li-auth/$p)"; done
{ echo "LOG=$LOG"; echo "ROLLBACK=sudo systemctl disable --now li-osint"; echo "DECISION=READY"; } | tee "$PTR"
