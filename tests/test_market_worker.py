"""Symbol-master scheduling in the market worker loop, on a fake monotonic clock."""
from types import SimpleNamespace

import pytest
from app import market_worker as mw


class Stop(Exception):
    pass


def drive(monkeypatch, tmp_path, times, kr=None, us=None):
    """Run main() once per value in `times` (the monotonic clock of that iteration) and return the refresh calls."""
    calls, now, later = [], [times[0]], iter(times[1:])
    def next_refresh(timeout=1):
        try: now[0] = next(later)
        except StopIteration: raise Stop
    def refresher(name, behaviour):
        def refresh():
            calls.append((name, now[0]))
            return behaviour() if behaviour else 1
        return refresh
    # main() writes MARKET_WORKER_MODE; setenv first so monkeypatch restores the old (unset) value.
    monkeypatch.setenv('MARKET_WORKER_MODE', '')
    monkeypatch.setattr(mw.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(mw, 'HEARTBEAT', tmp_path / 'heartbeat')
    monkeypatch.setattr(mw, 'MultiMarket', lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(mw, 'refresh_master', refresher('kr', kr))
    monkeypatch.setattr(mw, 'refresh_us_master', refresher('us', us))
    monkeypatch.setattr(mw.redis_cache, 'next_refresh', next_refresh)
    monkeypatch.setattr(mw.redis_cache, 'requested_symbols', lambda: [])
    with pytest.raises(Stop):
        mw.main()
    return calls


def test_masters_refresh_at_startup_even_right_after_host_boot(monkeypatch, tmp_path):
    # monotonic() counts from host boot, so 100 s means the host has just started.
    assert drive(monkeypatch, tmp_path, [100.0, 160.0]) == [('kr', 100.0), ('us', 100.0)]


def test_masters_refresh_daily_after_startup(monkeypatch, tmp_path):
    start = 200000.0
    calls = drive(monkeypatch, tmp_path, [start, start + mw.MASTER_REFRESH - 1, start + mw.MASTER_REFRESH + 1])
    assert calls == [('kr', start), ('us', start), ('kr', start + mw.MASTER_REFRESH + 1), ('us', start + mw.MASTER_REFRESH + 1)]


def test_failed_master_download_is_retried_after_an_hour(monkeypatch, tmp_path):
    def fail(): raise RuntimeError('download failed')
    start = 100.0
    calls = drive(monkeypatch, tmp_path, [start, start + mw.MASTER_RETRY - 1, start + mw.MASTER_RETRY + 1], kr=fail)
    assert [c for c in calls if c[0] == 'kr'] == [('kr', start), ('kr', start + mw.MASTER_RETRY + 1)]
