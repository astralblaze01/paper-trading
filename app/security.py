from collections import deque
from threading import Lock
import time

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
