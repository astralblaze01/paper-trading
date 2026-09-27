"""scripts/check_publication.py must scan binary files instead of stopping at them."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'check_publication.py'
SECRET = 'fixture-api-key-value-123'


def run_check(tmp_path, files):
    """Run the script in a throwaway repo; a stub git lists exactly `files`."""
    (tmp_path / 'scripts').mkdir()
    shutil.copy(SCRIPT, tmp_path / 'scripts' / 'check_publication.py')
    (tmp_path / '.env').write_text(f'FINNHUB_API_KEY={SECRET}\n')
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    listing = tmp_path / 'listing'
    listing.write_bytes(b''.join(name.encode() + b'\0' for name in files))
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    git = bin_dir / 'git'
    git.write_text(f'#!{sys.executable}\nimport sys\nsys.stdout.buffer.write(open({str(listing)!r},"rb").read())\n')
    git.chmod(0o755)
    return subprocess.run([sys.executable, str(tmp_path / 'scripts' / 'check_publication.py')], capture_output=True,
                          text=True, env={'PATH': f'{bin_dir}:/usr/bin:/bin'})


def test_binary_file_is_scanned_not_a_crash(tmp_path):
    image = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x8c\x9d' + bytes(range(256))
    result = run_check(tmp_path, {'photo.jpg': image, 'README.md': 'plain text'.encode()})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['count'] == 2


def test_secret_inside_binary_file_is_reported_without_its_value(tmp_path):
    leak = b'\x89PNG\r\n\x1a\n\x8c' + SECRET.encode() + b'\xff\x00'
    result = run_check(tmp_path, {'leak.png': leak, 'README.md': 'plain text'.encode()})
    assert result.returncode == 1, result.stderr
    assert 'leak.png: local secret: FINNHUB_API_KEY' in result.stdout
    assert SECRET not in result.stdout + result.stderr
