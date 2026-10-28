"""Ends a stream process whose heartbeat has stopped, so Docker starts it again.

A failing healthcheck only marks the container unhealthy: `restart: unless-stopped`
restarts it when the process exits, never because it is unhealthy. On 2026-10-25
the KIS stream stopped touching its heartbeat but stayed alive, and was unhealthy
for three days. The watchdog runs in its own thread, so a stalled event loop
cannot stop it.
"""
import logging
import os
import threading
import time

log = logging.getLogger('watchdog')
STALL = 300   # the healthcheck fails at 120 s; the longest normal gap is a 60 s reconnect backoff
CHECK = 30    # seconds between looks at the heartbeat


def stalled(heartbeat, started, now=None, stall=STALL):
    """Seconds since the heartbeat (or the start, before the first one) when past `stall`, else None."""
    now = time.time() if now is None else now
    try:
        last = max(started, heartbeat.stat().st_mtime)
    except FileNotFoundError:
        last = started
    age = now - last
    return age if age > stall else None


def watch(heartbeat, stall=STALL, check=CHECK, exit=os._exit):
    started = time.time()

    def loop():
        while True:
            time.sleep(check)
            age = stalled(heartbeat, started, stall=stall)
            if age is not None:
                log.error('heartbeat stopped %ss ago; exiting so the container restarts', int(age))
                for handler in logging.getLogger().handlers:
                    handler.flush()
                exit(1)
                return
    thread = threading.Thread(target=loop, name='watchdog', daemon=True)
    thread.start()
    return thread
