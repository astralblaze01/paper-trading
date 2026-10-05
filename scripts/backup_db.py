"""Dump the database to backups/daily-YYYYMMDD.dump once a day (kept 30 days by prune_backups.py).

The dump is written to a temporary file, checked with pg_restore --list, and only
then renamed into place, so a failed or partial dump never looks like a backup.
Files are readable by the owner only. Run daily from cron, before the prune:

    7 4 * * * /usr/bin/python3 /home/ubuntu/paper-trading/scripts/backup_db.py
"""
from pathlib import Path
import argparse
import os
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ['docker', 'compose', '--project-directory', str(ROOT)]
# Compose resolves a relative COMPOSE_FILE in .env (compose.yaml:compose.https.yaml)
# against the working folder, and cron starts this script in the home folder.


def backup(folder, run=subprocess.run, now=None):
    """Write today's dump into `folder`; returns its path. Raises if the dump or its check fails."""
    folder.mkdir(mode=0o700, exist_ok=True)
    target = folder / time.strftime('daily-%Y%m%d.dump', time.localtime(now))
    partial = target.with_name(target.name + '.partial')
    try:
        with open(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'wb') as out:
            run(COMPOSE + ['exec', '-T', 'db', 'pg_dump', '-U', 'paper', '-Fc', 'paper'], stdout=out, check=True, timeout=600, cwd=ROOT)
        with open(partial, 'rb') as dump:
            run(COMPOSE + ['exec', '-T', 'db', 'pg_restore', '--list'], stdin=dump, stdout=subprocess.DEVNULL, check=True, timeout=600, cwd=ROOT)
        if partial.stat().st_size == 0: raise RuntimeError('empty dump')
        partial.replace(target)
        return target
    finally:
        partial.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--dir', default=ROOT / 'backups', type=Path)
    args = parser.parse_args()
    path = backup(args.dir)
    print(f'{time.strftime("%Y-%m-%d %H:%M:%S")} wrote {path.name} ({path.stat().st_size} bytes)')
