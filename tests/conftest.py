# Minimal conftest for podrun tests.

import pytest

from podrun.podrun import load_podman_flags

_COV_THRESHOLD = 95


def _is_full_run(config):
    """True when no ``-k``, ``-m``, or explicit file/dir arguments were passed.

    ``file_or_dir`` is always populated (from ``testpaths`` when no args are
    given), so we compare it against the configured testpaths to distinguish
    "no args" from "specific files passed".
    """
    if config.option.keyword or config.option.markexpr:
        return False
    testpaths = [p.rstrip('/') for p in (config.getini('testpaths') or [])]
    given = [p.rstrip('/') for p in (config.option.file_or_dir or [])]
    return sorted(given) == sorted(testpaths)


def pytest_configure(config):
    """Enforce --cov-fail-under only on full-suite runs."""
    if _is_full_run(config):
        # Set on config.option (for any code that reads it directly)
        config.option.cov_fail_under = _COV_THRESHOLD
        # Also set on the CovPlugin's options namespace, which pytest-cov
        # captures from early_config before conftest hooks run.
        cov_plugin = config.pluginmanager.get_plugin('_cov')
        if cov_plugin is not None:
            cov_plugin.options.cov_fail_under = _COV_THRESHOLD


def pytest_terminal_summary(terminalreporter, config):
    """Print a coverage note when running a subset of tests."""
    if not _is_full_run(config):
        terminalreporter.write_line(
            f'Note: coverage threshold ({_COV_THRESHOLD}%) not enforced (subset of tests selected)',
            yellow=True,
        )


@pytest.fixture(autouse=True, scope='session')
def _require_podman_flags():
    """Skip the entire test session if podman flags cannot be loaded.

    This succeeds when either:
    - A local (non-remote) podman binary is available, OR
    - A podrun-started container has the cache file copy-mounted in
    """
    try:
        load_podman_flags()
    except SystemExit:
        pytest.skip('podman flags unavailable (remote client without cache)')
