"""Explicit operator commands. Never invoked by normal application startup."""
import argparse
import os
from pathlib import Path
import re
import socket
import subprocess
import urllib.request
import urllib.error

root=Path(__file__).resolve().parents[1]
values={}
for line in (root/'.env').read_text().splitlines():
    if line.strip() and not line.lstrip().startswith('#') and '=' in line:
        k,v=line.split('=',1); values[k]=v.strip().strip('"').strip("'")
domain=values.get('DOMAIN','')
if not re.fullmatch(r'(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}',domain):
    raise SystemExit('Set DOMAIN in .env to your actual DNS domain; no URL or port.')
parser=argparse.ArgumentParser(); parser.add_argument('action',choices=['check','bootstrap','issue','enable','hsts']); parser.add_argument('--confirmed-public-routing',action='store_true'); args=parser.parse_args()
base=['docker','compose','-f','compose.yaml','-f','compose.https.yaml']
def run(cmd): subprocess.run(cmd,cwd=root,check=True)
if args.action=='check':
    print('DNS:',sorted({r[4][0] for r in socket.getaddrinfo(domain,80)}))
    print('Verify these are your public IP addresses and inbound TCP 80/443 are forwarded. This script cannot confirm router settings.')
else:
    if not args.confirmed_public_routing: raise SystemExit('Confirm DNS and external port forwarding first; pass --confirmed-public-routing only after verification.')
    if args.action=='bootstrap': run(base+['-f','compose.bootstrap.yaml','up','-d','nginx'])
    if args.action=='issue':
        # An HTTP 404 is insufficient: a router can forward port 80 to the app's
        # LAN 8080 listener, which also returns 404 for unknown ACME paths.
        run(base+['run','--rm','--entrypoint','/bin/sh','certbot','-c',
                  'mkdir -p /var/www/certbot/.well-known/acme-challenge && printf paper-harbor-acme-ready > /var/www/certbot/.well-known/acme-challenge/paper-harbor-preflight'])
        probe='http://'+domain+'/.well-known/acme-challenge/paper-harbor-preflight'
        try:
            with urllib.request.urlopen(probe,timeout=10) as response:
                if response.read(100)!=b'paper-harbor-acme-ready':
                    raise SystemExit('External TCP 80 does not serve the Pi ACME volume. Forward it to Pi TCP 80, not 8080.')
        except urllib.error.HTTPError as exc:
            raise SystemExit('External TCP 80 returned '+str(exc.code)+'; forward it to Pi TCP 80, not 8080.')
        email=values.get('LETSENCRYPT_EMAIL','')
        contact=['--email',email] if email else ['--register-unsafely-without-email']
        run(base+['run','--rm','certbot','certonly','--webroot','-w','/var/www/certbot','--cert-name',domain,'-d',domain,*contact,'--agree-tos','--non-interactive'])
    if args.action=='enable': run(base+['up','-d','web','nginx','renew'])
    if args.action=='hsts':
        run(['curl','--fail','--silent','--show-error','https://'+domain+'/health'])
        (root/'nginx/tls/hsts.conf').write_text('add_header Strict-Transport-Security "max-age=31536000" always;\n')
        run(base+['exec','-T','nginx','nginx','-t']); run(base+['exec','-T','nginx','nginx','-s','reload'])
