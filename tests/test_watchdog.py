"""The stream watchdog: a process whose heartbeat stops exits, so Docker restarts it."""
import os

from app import watchdog


def test_stalled_measures_from_the_heartbeat_or_the_start(tmp_path):
    beat = tmp_path / 'heartbeat'
    # No heartbeat yet: the start counts as the last one.
    assert watchdog.stalled(beat, started=1000, now=1300) is None
    assert watchdog.stalled(beat, started=1000, now=1301) == 301
    beat.touch()
    os.utime(beat, (2000, 2000))
    assert watchdog.stalled(beat, started=1000, now=2300) is None
    assert watchdog.stalled(beat, started=1000, now=2400) == 400
    # A heartbeat older than this process (left by the last one) does not count.
    assert watchdog.stalled(beat, started=3000, now=3100) is None


def test_watch_exits_once_the_heartbeat_stops(tmp_path):
    beat, exits = tmp_path / 'heartbeat', []
    beat.touch()
    os.utime(beat, (0, 0))
    thread = watchdog.watch(beat, stall=-1, check=0, exit=exits.append)
    thread.join(2)
    assert exits == [1] and not thread.is_alive()

