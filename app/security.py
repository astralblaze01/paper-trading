from collections import deque
from threading import Lock
import hashlib
import hmac
import time
import os
from ipaddress import ip_address

def client_ip(request):
    """Trust the edge's overwritten header only in explicitly private deployments."""
    peer = request.client.host if request.client else 'unknown'
    if os.getenv('TRUST_PROXY_HEADERS', 'false').lower() == 'true':
        try:
            return str(ip_address(request.headers.get('x-real-ip', '')))
        except ValueError:
            pass
    return peer

class RateLimiter:
    def __init__(self): self.entries={}; self.lock=Lock()
    def allow(self,key,limit,seconds=60):
        with self.lock:
            now=time.monotonic()
            if len(self.entries)>20000:
                self.entries={k:v for k,v in self.entries.items() if v and now-v[-1]<seconds}
            values=self.entries.setdefault(key,deque())
            while values and now-values[0]>=seconds: values.popleft()
            if len(values)>=limit: return False
            values.append(now); return True
    def clear(self):
        with self.lock: self.entries.clear()

limiter=RateLimiter()

# The SSE stream re-checks the session cookie by hand, so it must agree with
# the SessionMiddleware settings in main.
SESSION_COOKIE='paper_session'
SESSION_MAX_AGE=43200

# The scheduler container authenticates to /internal/jobs with this header. It
# has only SESSION_SECRET, so this module must stay free of app imports.
WORKER_TOKEN_HEADER='x-worker-token'

def worker_token(secret): return hmac.new(secret.encode(),b'paper-worker',hashlib.sha256).hexdigest()
