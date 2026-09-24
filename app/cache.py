"""Bounded process cache: one web worker shares results across all browsers/jobs."""
import time
from threading import RLock

class TTLCache:
    def __init__(self, limit=2048): self.values={}; self.lock=RLock(); self.limit=limit
    def get(self, key, ttl, load):
        with self.lock:
            item=self.values.get(key)
            if item and time.monotonic()-item[0]<ttl: return item[1]
            result=load()
            if len(self.values)>=self.limit: self.values.clear()
            self.values[key]=(time.monotonic(),result)
            return result
