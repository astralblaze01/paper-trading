"""Where this deployment keeps its data: DATA_DIR, the same setting compose.yaml reads.

The host scripts run from cron, which does not load .env, so DATA_DIR is taken from
the environment first and then from the project's .env. A relative value is read
against the project folder, as Compose does.
"""
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parents[1]


def data_dir(root=ROOT, environ=None):
    """The data folder, or None when DATA_DIR is not set anywhere."""
    value = (os.environ if environ is None else environ).get('DATA_DIR', '').strip()
    if not value:
        try:
            for line in (root / '.env').read_text().splitlines():
                if line.startswith('DATA_DIR='):
                    value = line.split('=', 1)[1].strip().strip('\'"')
                    break
        except OSError:
            pass
    return root / value if value else None


def data_path(name, fallback, root=ROOT, environ=None):
    """`name` inside the data folder; `fallback` when DATA_DIR is not set."""
    folder = data_dir(root, environ)
    return folder / name if folder else fallback
