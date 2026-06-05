#!/usr/bin/env python3
"""Resume all paused mam-* torrents that are at 100% completion."""
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

def api_post(cn, cv, path, data):
    encoded = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(f'http://localhost:8082{path}', data=encoded)
    req.add_header('Cookie', f'{cn}={cv}')
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    resp = urllib.request.urlopen(req)
    return resp.read().decode()

cn, cv = login()
req = urllib.request.Request('http://localhost:8082/api/v2/torrents/info?filter=paused')
req.add_header('Cookie', f'{cn}={cv}')
torrents = json.loads(urllib.request.urlopen(req).read())

mam = [t for t in torrents if t.get('category','').startswith('mam')]
complete = [t for t in mam if t.get('progress', 0) >= 0.999]

hashes = '|'.join(t['hash'] for t in complete)
api_post(cn, cv, '/api/v2/torrents/start', {'hashes': hashes})

print(f'Resumed {len(complete)} torrents:')
for t in sorted(complete, key=lambda x: x['name']):
    print(f'  {t["name"]}')
