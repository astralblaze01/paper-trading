"""scripts/data_paths.py finds DATA_DIR the way compose.yaml does, for the cron scripts."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('data_paths', Path(__file__).resolve().parents[1] / 'scripts' / 'data_paths.py')
data_paths = importlib.util.module_from_spec(spec)
spec.loader.exec_module(data_paths)


def test_the_environment_wins_over_env_file(tmp_path):
    (tmp_path / '.env').write_text('DATA_DIR=/from/file\n')
    assert data_paths.data_dir(tmp_path, {'DATA_DIR': '/from/env'}) == Path('/from/env')


def test_cron_reads_the_env_file(tmp_path):
    (tmp_path / '.env').write_text('PUBLIC_URL=https://x\nDATA_DIR="/mnt/data/paper-trading"\n')
    assert data_paths.data_dir(tmp_path, {}) == Path('/mnt/data/paper-trading')


def test_a_relative_folder_is_read_against_the_project(tmp_path):
    assert data_paths.data_dir(tmp_path, {'DATA_DIR': './data'}) == tmp_path / 'data'


def test_unset_keeps_the_old_place(tmp_path):
    assert data_paths.data_dir(tmp_path, {}) is None
    (tmp_path / '.env').write_text('DATA_DIR=\n')
    assert data_paths.data_path('backups', tmp_path / 'backups', tmp_path, {}) == tmp_path / 'backups'
    assert data_paths.data_path('backups', None, tmp_path, {'DATA_DIR': '/d'}) == Path('/d/backups')
