# Minimal conftest for podrun tests.

import glob
import os

import pytest

import podrun.podrun as podrun_mod
from podrun.podrun import _read_flags_cache, load_podman_flags

_COV_THRESHOLD = 95

# Locate the podman flags cache file (e.g. ~/.cache/podrun/podman-4.5.0.json).
# This is needed because tests run in an environment without a local podman
# binary — the cache file was pre-built on the host.
_CACHE_DIR = os.path.join(
    os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache'),
    'podrun',
)
_CACHE_FILES = sorted(glob.glob(os.path.join(_CACHE_DIR, 'podman-*.json')))
_REMOTE_CACHE_FILES = sorted(glob.glob(os.path.join(_CACHE_DIR, 'podman-remote-*.json')))


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
    """Ensure podman flags are available for the test session.

    Resolution:
    1. Try ``load_podman_flags()`` normally (works with local podman or
       pre-mounted cache inside a podrun container).
    2. Fall back to seeding the in-memory cache from the host's
       ``~/.cache/podrun/podman-*.json`` file.  This enables unit tests
       in environments with only ``podman-remote`` (no local podman).
    3. Skip the session if neither succeeds.
    """
    try:
        load_podman_flags()
        return
    except SystemExit:
        pass

    # Seed from host cache file
    if _CACHE_FILES:
        flags = _read_flags_cache(_CACHE_FILES[-1])
        if flags is not None:
            podrun_mod._loaded_flags['podman'] = flags
            # Also seed podman-remote cache (from dedicated cache or podman's)
            if _REMOTE_CACHE_FILES:
                remote_flags = _read_flags_cache(_REMOTE_CACHE_FILES[-1])
                if remote_flags is not None:
                    podrun_mod._loaded_flags['podman-remote'] = remote_flags
            return

    pytest.skip('podman flags unavailable (no local podman and no cache file)')
