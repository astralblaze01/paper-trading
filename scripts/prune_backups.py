"""Delete database dumps in backups/ once they are 30 days old (privacy policy, section 4).

Only dump files are touched; source archives and anything else in the folder stay.
Run daily from cron:

    17 4 * * * /usr/bin/python3 /home/ubuntu/paper-trading/scripts/prune_backups.py
"""
from pathlib import Path
import argparse
import time

DUMP_SUFFIXES = ('.dump', '.sql', '.dump.gz', '.sql.gz')
DAY = 86400


def prune(folder, days=30, now=None):
    """Remove dumps whose modification time is at least `days` old; returns the removed names."""
    cutoff = (now if now is not None else time.time()) - days * DAY
    removed = []
    for path in sorted(Path(folder).iterdir()):
        if path.is_symlink() or not path.is_file() or not path.name.endswith(DUMP_SUFFIXES):
            continue
        if path.stat().st_mtime <= cutoff:
            path.unlink()
            removed.append(path.name)
    return removed


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--dir', default=Path(__file__).resolve().parents[1] / 'backups', type=Path)
    parser.add_argument('--days', default=30, type=int)
    args = parser.parse_args()
    if args.dir.is_dir():
        for name in prune(args.dir, args.days):
            print(f'{time.strftime("%Y-%m-%d %H:%M:%S")} removed {name}')
