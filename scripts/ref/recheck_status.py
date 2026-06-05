#!/usr/bin/env python3
import json, urllib.request, urllib.parse

def login():
    data = urllib.parse.urlencode({'username': 'admin', 'password': 'ctzlyLkXPSOX3r'}).encode()
    req = urllib.request.Request('http://localhost:8082/api/v2/auth/login', data=data)
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    resp = urllib.request.urlopen(req)
    cookies = resp.headers.get_all('Set-Cookie') or [resp.headers.get('Set-Cookie', '')]
    for cookie in cookies:
        for part in cookie.split(';'):
            part = part.strip()
            if '=' in part and 'SID' in part.upper():
                n, v = part.split('=', 1)
                return n, v

cn, cv = login()
req = urllib.request.Request('http://localhost:8082/api/v2/torrents/info?filter=paused')
req.add_header('Cookie', f'{cn}={cv}')
torrents = json.loads(urllib.request.urlopen(req).read())
mam = [t for t in torrents if t.get('category','').startswith('mam')]
checking = [t for t in mam if t.get('state') in ('checkingUP', 'checkingDL', 'checking')]
complete = [t for t in mam if t.get('progress', 0) >= 0.999]
partial = [t for t in mam if 0.001 < t.get('progress', 0) < 0.999]
zero = [t for t in mam if t.get('progress', 0) < 0.001]
print(f'Still checking: {len(checking)}')
print(f'Complete (100%): {len(complete)}')
print(f'Partial: {len(partial)}')
print(f'Zero (0%): {len(zero)}')
print()
print('--- COMPLETE ---')
for t in sorted(complete, key=lambda x: x['name']):
    print(f'  {t["name"]}')
print()
print('--- PARTIAL ---')
for t in sorted(partial, key=lambda x: -x['progress']):
    print(f'  {t["progress"]*100:.1f}%  {t["name"]}')
