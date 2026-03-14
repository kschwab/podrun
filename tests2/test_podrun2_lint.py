"""Tests for Phase 2.8 — linting + coverage."""

import os
import shutil
import subprocess
import sys
import tempfile

import pytest

import podrun.podrun2 as podrun2_mod
from podrun.podrun2 import (
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

_TARGETS = ['podrun/podrun2.py', 'tests2/']


# ---------------------------------------------------------------------------
# Fixture: isolate PODRUN_TMP for entrypoint generators
# ---------------------------------------------------------------------------


@pytest.fixture()
def _isolate_tmp(tmp_path, monkeypatch):
    """Redirect PODRUN_TMP so generators don't write to the real runtime dir."""
    monkeypatch.setattr(podrun2_mod, 'PODRUN_TMP', str(tmp_path))


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
            [mypy, 'podrun/podrun2.py'],
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
        monkeypatch.setattr(podrun2_mod, 'run_os_cmd', lambda cmd: mock_result)

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
        from podrun.podrun2 import _generate_bash_completion

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
        from podrun.podrun2 import _generate_zsh_completion

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
        """Detect unused code in podrun/podrun2.py."""
        result = subprocess.run(
            [sys.executable, '-m', 'vulture', 'podrun/podrun2.py', 'podrun2_whitelist.py'],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        assert result.returncode == 0, f'Dead code found:\n{result.stdout}'


# ---------------------------------------------------------------------------
# Coverage threshold
# ---------------------------------------------------------------------------


class TestCoverage:
    def test_minimum_coverage(self, tmp_path):
        """Ensure podrun2.py line coverage stays above 90%."""
        # Write a minimal coverage config that targets podrun2.py instead of
        # the default pyproject.toml config (which targets podrun.py only).
        cov_cfg = tmp_path / '.coveragerc'
        cov_cfg.write_text(
            '[run]\nsource = podrun\nomit = */test*\n\n'
            '[report]\ninclude = podrun/podrun2.py\nshow_missing = true\n'
        )
        result = subprocess.run(
            [
                sys.executable,
                '-m',
                'pytest',
                'tests2/',
                '--timeout=10',
                '--cov=podrun',
                '--cov-report=term',
                '--cov-fail-under=90',
                f'--cov-config={cov_cfg}',
                '-q',
                '--no-header',
                '--override-ini=addopts=',
                '-p',
                'no:cacheprovider',
                # Exclude this file to avoid recursion
                '--ignore=tests2/test_podrun2_lint.py',
            ],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        # Extract coverage line from output
        cov_line = ''
        for line in result.stdout.splitlines():
            if 'podrun2' in line and '%' in line:
                cov_line = line
                break
        assert result.returncode == 0, (
            f'Coverage below 90% threshold.\n{cov_line}\n\n'
            f'stdout:\n{result.stdout[-500:]}\n'
            f'stderr:\n{result.stderr[-500:]}'
        )
