"""Scan publishable source against local secrets without printing secret values."""
from pathlib import Path
import json,re,subprocess,sys
root=Path(__file__).resolve().parents[1]
paths=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=root).decode().split('\0')
paths=sorted({p for p in paths if p and (root/p).is_file()})
secrets=[]
if (root/'.env').exists():
    for line in (root/'.env').read_text().splitlines():
        if '=' not in line or line.lstrip().startswith('#'):continue
        key,value=line.split('=',1);value=value.strip().strip('\"').strip("'")
        if any(x in key.upper() for x in ('PASSWORD','SECRET','TOKEN','API_KEY')) and len(value)>=8:secrets.append((key,value.encode()))
issues=[]
for name in paths:
    p=Path(name)
    if (p.name.startswith('.env') and p.name!='.env.example') or p.suffix in ('.pem','.key','.dump','.sql','.swp','.log') or any(x in p.parts for x in ('backups','artifacts','pgdata','certificates','.codex','.agents')):
        issues.append((name,'private file'));continue
    # Bytes, not text: an image or archive can carry a secret too, and must not stop the scan.
    data=(root/name).read_bytes()
    for key,value in secrets:
        if value in data:issues.append((name,'local secret: '+key))
    if re.search(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',data):issues.append((name,'private key'))
if issues:
    for name,kind in issues:print(name+': '+kind)
    sys.exit(1)
print(json.dumps({'files':paths,'count':len(paths),'status':'no local secret values or private artifacts found'},ensure_ascii=False))
