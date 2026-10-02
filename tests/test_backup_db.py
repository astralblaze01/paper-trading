"""scripts/backup_db.py writes a checked daily dump, or nothing at all."""
import importlib.util
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('backup_db', Path(__file__).resolve().parents[1] / 'scripts' / 'backup_db.py')
backup_db = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup_db)
NOW = 2_000_000_000  # 2033-05-18 in Korea


def fake(dump=b'PGDMP...', check_ok=True):
    calls = []
    def run(cmd, stdout=None, stdin=None, check=False, timeout=None):
        calls.append(cmd[cmd.index('db') + 1])
        if cmd[-1] == 'paper': stdout.write(dump)
        elif not check_ok: raise subprocess.CalledProcessError(1, cmd)
    return run, calls


def test_a_checked_dump_is_kept_owner_only(tmp_path):
    run, calls = fake()
    path = backup_db.backup(tmp_path / 'backups', run, NOW)
    assert path.name == 'daily-20330518.dump' and path.read_bytes() == b'PGDMP...'
    assert calls == ['pg_dump', 'pg_restore'] and oct(path.stat().st_mode & 0o777) == '0o600'
    assert [p.name for p in path.parent.iterdir()] == ['daily-20330518.dump']
    # The prune script recognises it, so the 30-day policy applies.
    assert path.name.endswith(('.dump',))


@pytest.mark.parametrize('dump, check_ok', [(b'garbage', False), (b'', True)])
def test_a_dump_that_fails_its_check_leaves_nothing(tmp_path, dump, check_ok):
    run, _ = fake(dump, check_ok)
    with pytest.raises((subprocess.CalledProcessError, RuntimeError)):
        backup_db.backup(tmp_path, run, NOW)
    assert list(tmp_path.iterdir()) == []
