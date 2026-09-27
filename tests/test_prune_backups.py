"""scripts/prune_backups.py keeps the privacy policy's 30-day backup promise."""
import importlib.util
import os
from pathlib import Path

spec = importlib.util.spec_from_file_location('prune_backups', Path(__file__).resolve().parents[1] / 'scripts' / 'prune_backups.py')
prune_backups = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prune_backups)
DAY = 86400
NOW = 2_000_000_000


def make(folder, name, age_days):
    path = folder / name
    path.write_bytes(b'x')
    os.utime(path, (NOW - age_days * DAY, NOW - age_days * DAY))
    return path


def test_only_dumps_thirty_days_old_are_removed(tmp_path):
    make(tmp_path, 'old.dump', 30)
    make(tmp_path, 'older.sql.gz', 90)
    make(tmp_path, 'recent.dump', 29.9)
    make(tmp_path, 'source-before-x.tar.gz', 400)   # not a database dump
    make(tmp_path, 'notes.txt', 400)
    (tmp_path / 'nested').mkdir()
    make(tmp_path / 'nested', 'deep.dump', 400)       # only the folder itself is pruned
    assert prune_backups.prune(tmp_path, 30, now=NOW) == ['old.dump', 'older.sql.gz']
    assert sorted(p.name for p in tmp_path.iterdir()) == ['nested', 'notes.txt', 'recent.dump', 'source-before-x.tar.gz']
    assert (tmp_path / 'nested' / 'deep.dump').exists()


def test_symlinked_dump_is_left_alone(tmp_path):
    target = make(tmp_path, 'keep.bin', 400)
    (tmp_path / 'link.dump').symlink_to(target)
    os.utime(tmp_path / 'link.dump', (NOW - 400 * DAY,) * 2, follow_symlinks=False)
    assert prune_backups.prune(tmp_path, 30, now=NOW) == []
    assert target.exists()
