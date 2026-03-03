import os
import shutil
import subprocess
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestRuff:
    @pytest.mark.skipif(
        shutil.which('ruff') is None
        and not os.path.isfile(os.path.expanduser('~/.local/bin/ruff')),
        reason='ruff not available',
    )
    def test_ruff(self):
        ruff = shutil.which('ruff') or os.path.expanduser('~/.local/bin/ruff')
        result = subprocess.run(
            [ruff, 'check', 'podrun/', 'tests/'],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        assert result.returncode == 0, f'ruff errors:\n{result.stdout}'

    @pytest.mark.skipif(
        shutil.which('ruff') is None
        and not os.path.isfile(os.path.expanduser('~/.local/bin/ruff')),
        reason='ruff not available',
    )
    def test_ruff_format(self):
        ruff = shutil.which('ruff') or os.path.expanduser('~/.local/bin/ruff')
        result = subprocess.run(
            [ruff, 'format', '--check', 'podrun/', 'tests/'],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        assert result.returncode == 0, f'ruff format errors:\n{result.stdout}{result.stderr}'


class TestMypy:
    @pytest.mark.skipif(
        shutil.which('mypy') is None
        and not os.path.isfile(os.path.expanduser('~/.local/bin/mypy')),
        reason='mypy not available',
    )
    def test_mypy(self):
        mypy = shutil.which('mypy') or os.path.expanduser('~/.local/bin/mypy')
        result = subprocess.run(
            [mypy, 'podrun/'],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        assert result.returncode == 0, f'mypy errors:\n{result.stdout}'


class TestVulture:
    @pytest.mark.skipif(
        subprocess.run(
            [sys.executable, '-m', 'vulture', '--version'],
            capture_output=True,
        ).returncode
        != 0,
        reason='vulture not available',
    )
    def test_no_dead_code(self):
        """Detect unused code in podrun/podrun.py."""
        result = subprocess.run(
            [sys.executable, '-m', 'vulture', 'podrun/podrun.py'],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        assert result.returncode == 0, f'Dead code found:\n{result.stdout}'
