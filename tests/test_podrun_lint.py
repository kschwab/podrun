"""Tests for Phase 2.8 — linting + coverage."""

import os
import shutil
import subprocess
import sys
import tempfile

import pytest

import podrun.podrun as podrun_mod
from podrun.podrun import (
    generate_exec_entrypoint,
    generate_rc_sh,
    generate_run_entrypoint,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_RUFF_AVAILABLE = shutil.which('ruff') is not None or os.path.isfile(
    os.path.expanduser('~/.local/bin/ruff')
)
_MYPY_AVAILABLE = shutil.which('mypy') is not None or os.path.isfile(
    os.path.expanduser('~/.local/bin/mypy')
)
_SHELLCHECK_AVAILABLE = shutil.which('shellcheck') is not None
_VULTURE_AVAILABLE = (
    subprocess.run(
        [sys.executable, '-m', 'vulture', '--version'],
        capture_output=True,
    ).returncode
    == 0
)

_TARGETS = ['podrun/podrun.py', 'tests/']


# ---------------------------------------------------------------------------
# Fixture: isolate PODRUN_TMP for entrypoint generators
# ---------------------------------------------------------------------------


@pytest.fixture()
def _isolate_tmp(tmp_path, monkeypatch):
    """Redirect PODRUN_TMP so generators don't write to the real runtime dir."""
    monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path))


def _default_ns(**overrides):
    """Build a minimal ns dict for entrypoint generation."""
    ns = {
        'run.login': None,
        'run.shell': None,
        'run.export': [],
        'run.prompt_banner': None,
    }
    ns.update(overrides)
    return ns


# ---------------------------------------------------------------------------
# Ruff
# ---------------------------------------------------------------------------


class TestRuff:
    @pytest.mark.skipif(not _RUFF_AVAILABLE, reason='ruff not available')
    def test_ruff(self):
        ruff = shutil.which('ruff') or os.path.expanduser('~/.local/bin/ruff')
        result = subprocess.run(
            [ruff, 'check'] + _TARGETS,
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        assert result.returncode == 0, f'ruff errors:\n{result.stdout}'

    @pytest.mark.skipif(not _RUFF_AVAILABLE, reason='ruff not available')
    def test_ruff_format(self):
        ruff = shutil.which('ruff') or os.path.expanduser('~/.local/bin/ruff')
        result = subprocess.run(
            [ruff, 'format', '--check'] + _TARGETS,
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        assert result.returncode == 0, f'ruff format errors:\n{result.stdout}{result.stderr}'


# ---------------------------------------------------------------------------
# Mypy
# ---------------------------------------------------------------------------


class TestMypy:
    @pytest.mark.skipif(not _MYPY_AVAILABLE, reason='mypy not available')
    def test_mypy(self):
        mypy = shutil.which('mypy') or os.path.expanduser('~/.local/bin/mypy')
        result = subprocess.run(
            [mypy, 'podrun/podrun.py'],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        assert result.returncode == 0, f'mypy errors:\n{result.stdout}'


# ---------------------------------------------------------------------------
# Shellcheck
# ---------------------------------------------------------------------------


class TestShellcheck:
    @pytest.mark.skipif(not _SHELLCHECK_AVAILABLE, reason='shellcheck not available')
    @pytest.mark.usefixtures('_isolate_tmp')
    def test_run_entrypoint(self):
        path = generate_run_entrypoint(_default_ns())
        result = subprocess.run(
            ['shellcheck', '-s', 'sh', '--severity=warning', path],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f'shellcheck errors:\n{result.stdout}'

    @pytest.mark.skipif(not _SHELLCHECK_AVAILABLE, reason='shellcheck not available')
    @pytest.mark.usefixtures('_isolate_tmp')
    def test_rc_sh(self, monkeypatch):
        # Stub run_os_cmd to avoid real subprocess calls for CPU info
        mock_result = subprocess.CompletedProcess(args='', returncode=0, stdout='', stderr='')
        monkeypatch.setattr(podrun_mod, 'run_os_cmd', lambda cmd: mock_result)

        path = generate_rc_sh(_default_ns())
        result = subprocess.run(
            ['shellcheck', '-s', 'sh', '--severity=warning', path],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f'shellcheck errors:\n{result.stdout}'

    @pytest.mark.skipif(not _SHELLCHECK_AVAILABLE, reason='shellcheck not available')
    @pytest.mark.usefixtures('_isolate_tmp')
    def test_exec_entrypoint(self):
        path = generate_exec_entrypoint()
        result = subprocess.run(
            ['shellcheck', '-s', 'sh', '--severity=warning', path],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f'shellcheck errors:\n{result.stdout}'

    @pytest.mark.skipif(not _SHELLCHECK_AVAILABLE, reason='shellcheck not available')
    def test_bash_completion(self):
        from podrun.podrun import _generate_bash_completion

        content = _generate_bash_completion()
        with tempfile.NamedTemporaryFile(mode='w', suffix='.bash', delete=False) as f:
            f.write(content)
            path = f.name
        try:
            result = subprocess.run(
                ['shellcheck', '-s', 'bash', '--severity=warning', path],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, f'shellcheck errors:\n{result.stdout}'
        finally:
            os.unlink(path)

    @pytest.mark.skipif(not _SHELLCHECK_AVAILABLE, reason='shellcheck not available')
    def test_zsh_completion(self):
        """Zsh completion — shellcheck as bash (zsh not natively supported).

        Uses --severity=error since zsh-specific constructs (compstate, words)
        trigger false positive warnings in bash mode.
        """
        from podrun.podrun import _generate_zsh_completion

        content = _generate_zsh_completion()
        with tempfile.NamedTemporaryFile(mode='w', suffix='.zsh', delete=False) as f:
            f.write(content)
            path = f.name
        try:
            result = subprocess.run(
                ['shellcheck', '-s', 'bash', '--severity=error', path],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, f'shellcheck errors:\n{result.stdout}'
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Vulture
# ---------------------------------------------------------------------------


class TestVulture:
    @pytest.mark.skipif(not _VULTURE_AVAILABLE, reason='vulture not available')
    def test_no_dead_code(self):
        """Detect unused code in podrun/podrun.py."""
        result = subprocess.run(
            [sys.executable, '-m', 'vulture', 'podrun/podrun.py', 'podrun_whitelist.py'],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        assert result.returncode == 0, f'Dead code found:\n{result.stdout}'
