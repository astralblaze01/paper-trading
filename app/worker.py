"""Separate scheduler, sharing the web process's provider cache through an internal API."""
import os
import time
from pathlib import Path
import httpx
import logging
from .logging_config import configure_logging
from .security import WORKER_TOKEN_HEADER, worker_token

configure_logging()
log=logging.getLogger('scheduler')

if __name__=='__main__':
    token=worker_token(os.environ['SESSION_SECRET'])
    while True:
        try:
            r=httpx.post('http://web:8000/internal/jobs',headers={WORKER_TOKEN_HEADER:token},timeout=180)
            r.raise_for_status()
            Path('/tmp/worker-heartbeat').touch()
        except httpx.HTTPError:
            log.warning('scheduled work unavailable; retrying')
        time.sleep(60)
