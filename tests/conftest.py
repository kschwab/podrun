# Minimal conftest for podrun tests.

import glob
import os
import shutil
import subprocess
import sys

import pytest

if sys.platform == 'win32':
    pytest.skip('podrun tests require Linux', allow_module_level=True)

import podrun.podrun as podrun_mod
from podrun.podrun import (
    ENV_PODRUN_CONTAINER,
    ENV_PODRUN_PODMAN_PATH,
    ENV_PODRUN_PODMAN_REMOTE,
    _read_flags_cache,
    load_podman_flags,
)

_COV_THRESHOLD = 95

# Locate podman flags cache files (stat-based: podman-{mtime_ns}-{size}.json).
_CACHE_DIR = os.path.join(
    os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache'),
    'podrun',
)
_ALL_CACHE_FILES = sorted(glob.glob(os.path.join(_CACHE_DIR, 'podman-*.json')))
_REMOTE_CACHE_FILES = sorted(glob.glob(os.path.join(_CACHE_DIR, 'podman-remote-*.json')))
_PODMAN_CACHE_FILES = [f for f in _ALL_CACHE_FILES if f not in _REMOTE_CACHE_FILES]

# Session-level state set by _require_podman_flags.
_has_podman = False
_has_podman_remote = False
_podman_path: str = 'podman'


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


@pytest.hookimpl(tryfirst=True)
def pytest_terminal_summary(terminalreporter, config):
    """Print a coverage note when threshold is not enforced.

    Disables coverage enforcement when running a subset of tests OR when
    any tests were skipped (meaning a binary is missing and coverage cannot
    reach the threshold).
    """
    reason = None
    if not _is_full_run(config):
        reason = 'subset of tests selected'
    elif terminalreporter.stats.get('skipped'):
        reason = f'{len(terminalreporter.stats["skipped"])} tests skipped'
    if reason:
        # Disable threshold before pytest-cov's trylast hook checks it.
        config.option.cov_fail_under = 0
        cov_plugin = config.pluginmanager.get_plugin('_cov')
        if cov_plugin is not None:
            cov_plugin.options.cov_fail_under = 0
        terminalreporter.write_line(
            f'Note: coverage threshold ({_COV_THRESHOLD}%) not enforced ({reason})',
            yellow=True,
        )


def pytest_collection_modifyitems(config, items):
    """Deselect parameterized tests that can't run with the other binary.

    Tests using ``podman_only`` are deselected for ``[podman-remote]``.
    Tests using ``requires_podman_remote`` are deselected for ``[podman]``.
    This avoids noisy skips when both binaries are installed.
    """
    deselected = []
    remaining = []
    for item in items:
        cs = getattr(item, 'callspec', None)
        binary = cs.params.get('podman_binary') if cs else None
        if binary == 'podman-remote' and 'podman_only' in item.fixturenames:
            deselected.append(item)
        elif binary == 'podman' and 'requires_podman_remote' in item.fixturenames:
            deselected.append(item)
        else:
            remaining.append(item)
    if deselected:
        items[:] = remaining
        config.hook.pytest_deselected(items=deselected)


def _try_load(binary):
    """Try to load flags for *binary* (live scrape or disk cache).

    Returns True on success (flags are in ``_loaded_flags``).
    """
    try:
        load_podman_flags(binary)
        return True
    except (SystemExit, FileNotFoundError, RuntimeError):
        pass
    return False


def _try_seed_from_cache(binary, cache_files):
    """Seed in-memory flags from a disk cache file.  Returns True on success.

    Only seeds if the binary is actually installed — a stale cache from a
    since-removed binary should not make ``_has_podman*`` report True.
    """
    if not shutil.which(binary):
        return False
    if not cache_files:
        return False
    flags = _read_flags_cache(cache_files[-1])
    if flags is None:
        return False
    podrun_mod._loaded_flags[binary] = flags
    return True


@pytest.fixture(autouse=True, scope='session')
def _require_podman_flags():
    """Ensure podman flags are available for the test session.

    Tries ``podman`` and ``podman-remote`` independently (live scrape then
    disk cache).  Sets module-level ``_has_podman`` / ``_has_podman_remote``
    so fixtures can skip tests that need a specific binary.

    When only one binary is available, its flags are *also* stored under the
    other key so that ``parse_args()`` (which defaults to ``podman``) can
    still function — tests that use flags only present in the missing binary
    should use the ``podman_only`` fixture.
    """
    global _has_podman, _has_podman_remote, _podman_path

    # podman — scrape without CONTAINER_HOST so we get the full native flag
    # set.  When CONTAINER_HOST is set (e.g. inside a podrun container),
    # ``podman --help`` shows the reduced remote-mode flags, which would
    # prevent tests from exercising full-podman features like --root.
    saved_ch = os.environ.pop('CONTAINER_HOST', None)
    try:
        _has_podman = _try_load('podman') or _try_seed_from_cache('podman', _PODMAN_CACHE_FILES)
    finally:
        if saved_ch is not None:
            os.environ['CONTAINER_HOST'] = saved_ch

    # podman-remote
    _has_podman_remote = _try_load('podman-remote') or _try_seed_from_cache(
        'podman-remote', _REMOTE_CACHE_FILES
    )

    if not _has_podman and not _has_podman_remote:
        pytest.skip('podman flags unavailable (no podman or podman-remote)')

    # Cross-seed so parse_args()/main() work with default key.
    if _has_podman and not _has_podman_remote:
        podrun_mod._loaded_flags['podman-remote'] = podrun_mod._loaded_flags['podman']
    elif _has_podman_remote and not _has_podman:
        podrun_mod._loaded_flags['podman'] = podrun_mod._loaded_flags['podman-remote']

    # Resolve the primary podman binary path for tests that call main().
    _podman_path = shutil.which('podman') or shutil.which('podman-remote') or 'podman'


# ---------------------------------------------------------------------------
# Public fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope='session')
def podman_path():
    """The resolved podman binary path for this session."""
    return _podman_path


@pytest.fixture(scope='session')
def has_podman():
    """True when full podman (not just podman-remote) flags are available."""
    return _has_podman


@pytest.fixture(scope='session')
def has_podman_remote():
    """True when podman-remote flags are available."""
    return _has_podman_remote


@pytest.fixture(params=['podman', 'podman-remote'])
def podman_binary(request, monkeypatch):
    """Parameterized fixture: run the test once per available binary.

    Skips the parameter if its binary is unavailable.  Monkeypatches
    ``_default_podman_path`` so code under test resolves the correct binary.

    When testing with ``podman``, clears ``CONTAINER_HOST`` so that
    ``_is_remote()`` returns False and the full flag set is exercised.
    """
    binary = request.param
    path = shutil.which(binary)
    if binary == 'podman' and (not _has_podman or not path):
        pytest.skip('podman not available')
    if binary == 'podman-remote' and (not _has_podman_remote or not path):
        pytest.skip('podman-remote not available')
    assert path is not None  # for mypy; skip above ensures this
    monkeypatch.setattr(podrun_mod, '_default_podman_path', lambda: path)
    if binary == 'podman':
        monkeypatch.delenv('CONTAINER_HOST', raising=False)
    return binary


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Universal test isolation — applied to every test automatically.

    - Prevents real devcontainer.json / store dir discovery.
    - Redirects PODRUN_TMP so generated scripts go to a temp dir.
    - Clears PODRUN_* and CONTAINER_HOST env vars to prevent cross-test bleed.
    """
    monkeypatch.setattr(podrun_mod, 'find_devcontainer_json', lambda start_dir=None: None)
    monkeypatch.setattr(podrun_mod, '_default_store_dir', lambda: None)
    monkeypatch.setattr(podrun_mod, '_discover_podrunrc', lambda: None)
    monkeypatch.setattr(podrun_mod, '_resolve_script_command', podrun_mod._shell_quote)
    monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path))
    monkeypatch.delenv(ENV_PODRUN_PODMAN_REMOTE, raising=False)
    monkeypatch.delenv(ENV_PODRUN_CONTAINER, raising=False)
    monkeypatch.delenv(ENV_PODRUN_PODMAN_PATH, raising=False)
    monkeypatch.delenv('PODRUN_LOCAL_STORE', raising=False)
    monkeypatch.delenv('CONTAINER_HOST', raising=False)


@pytest.fixture
def mock_run_os_cmd(monkeypatch):
    """Monkeypatch podrun.run_os_cmd and return a controller.

    Only used for tests that need to simulate podman failure or control
    exact output.  Most tests use real podman.
    """

    class Controller:
        def __init__(self):
            self.calls = []
            self._return_value = None
            self._side_effect = None

        def set_return(self, stdout='', stderr='', returncode=0):
            self._return_value = subprocess.CompletedProcess(
                args='', returncode=returncode, stdout=stdout, stderr=stderr
            )
            self._side_effect = None

        def set_side_effect(self, effects):
            self._side_effect = list(effects)
            self._return_value = None

        def __call__(self, cmd, env=None):
            self.calls.append(cmd)
            if self._side_effect is not None:
                if self._side_effect:
                    val = self._side_effect.pop(0)
                else:
                    val = subprocess.CompletedProcess(args='', returncode=0, stdout='', stderr='')
                if isinstance(val, subprocess.CompletedProcess):
                    return val
                raise val
            if self._return_value is not None:
                return self._return_value
            return subprocess.CompletedProcess(args='', returncode=0, stdout='', stderr='')

    ctrl = Controller()
    monkeypatch.setattr(podrun_mod, 'run_os_cmd', ctrl)
    return ctrl


@pytest.fixture()
def podman_only(request):
    """Restrict a test to full podman (not podman-remote).

    When used with ``podman_binary``, the ``[podman-remote]`` parameterization
    is deselected at collection time by ``pytest_collection_modifyitems``
    (no skip emitted).  When used without ``podman_binary``, skips if podman
    is unavailable.
    """
    if 'podman_binary' not in request.fixturenames and not _has_podman:
        pytest.skip('requires podman (only podman-remote available)')


@pytest.fixture()
def requires_podman_remote(request):
    """Restrict a test to podman-remote.

    When used with ``podman_binary``, the ``[podman]`` parameterization is
    deselected at collection time by ``pytest_collection_modifyitems``.
    When used without ``podman_binary``, skips if podman-remote is unavailable.
    """
    if 'podman_binary' not in request.fixturenames and not _has_podman_remote:
        pytest.skip('requires podman-remote (only podman available)')
