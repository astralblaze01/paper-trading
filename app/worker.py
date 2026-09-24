"""Separate scheduler, sharing the web process's provider cache through an internal API."""
import hashlib
import hmac
import os
import time
from pathlib import Path
import httpx

if __name__=='__main__':
    token=hmac.new(os.environ['SESSION_SECRET'].encode(),b'paper-worker',hashlib.sha256).hexdigest()
    while True:
        try:
            r=httpx.post('http://web:8000/internal/jobs',headers={'x-worker-token':token},timeout=180)
            r.raise_for_status()
            Path('/tmp/worker-heartbeat').touch()
        except httpx.HTTPError:
            print('Scheduled work unavailable; retrying in 60 seconds',flush=True)
        time.sleep(60)
