import argparse
import dataclasses
import os
import pathlib
import shlex
import shutil
import subprocess
import sys

import filelock
import pytest

import podrun.podrun as podrun_mod
from podrun.podrun import Config, _runroot_path, main as podrun_main


FAKE_UID = 1234
FAKE_GID = 5678
FAKE_UNAME = 'testuser'
FAKE_USER_HOME = '/home/testuser'

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
PODRUN_STORE = PROJECT_ROOT / '.podrun-store'

# All base images used by the live test suite.  Eagerly pulled into the
# shared store so per-worker copies already contain them.
TEST_IMAGES = ['alpine:latest', 'ubuntu:24.04', 'fedora:latest']


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Enforce or warn about 100% coverage depending on whether all tests ran."""
    cov_plugin = config.pluginmanager.get_plugin('_cov')
    if cov_plugin is None or cov_plugin.cov_total is None:
        return
    n = _numprocesses(config)
    if n != 0:
        return
    if cov_plugin.cov_total >= 100:
        return
    skipped = terminalreporter.stats.get('skipped', [])
    deselected = terminalreporter.stats.get('deselected', [])
    if skipped or deselected:
        terminalreporter.write_line(
            'WARNING: test coverage of %.2f%% < 100%% '
            '(%d skipped, %d deselected — not enforcing)'
            % (cov_plugin.cov_total, len(skipped), len(deselected)),
            yellow=True,
            bold=True,
        )
    else:
        terminalreporter.write_line(
            'FAIL Required test coverage of 100%% not reached. '
            'Total coverage: %.2f%%' % cov_plugin.cov_total,
            red=True,
            bold=True,
        )
        config._cov_enforcement_failed = True


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    if getattr(session.config, '_cov_enforcement_failed', False):
        session.exitstatus = 1


def pytest_addoption(parser):
    """Add podrun-specific pytest command-line options."""
    parser.addoption(
        '--registry',
        default=None,
        help='Registry mirror for pulling images (e.g. my-mirror.example.com). '
        'Falls back to PODRUN_TEST_REGISTRY env var.',
    )


def _numprocesses(config):
    """Return the ``-n`` value as an int (0 when xdist is inactive).

    On xdist workers, ``config.getoption('numprocesses')`` returns ``None``
    because workers don't receive the ``-n`` flag.  Fall back to the
    ``PYTEST_XDIST_WORKER_COUNT`` env var that xdist sets on every worker
    during ``pytest_configure``.
    """
    try:
        n = config.getoption('numprocesses')
    except ValueError:
        return 0
    if n is None or str(n) == '0':
        wc = os.environ.get('PYTEST_XDIST_WORKER_COUNT')
        if wc is not None:
            return int(wc)
        return 0
    if str(n) == 'auto':
        return len(TEST_IMAGES)
    return max(1, int(n))


def _allowed_images(config):
    """Return the set of images allowed by the ``-n`` worker count.

    -n0  → all images (full serial suite)
    -n1  → alpine only (smoke)
    -n2  → alpine + ubuntu
    -n3+ → all images
    """
    n = _numprocesses(config)
    if n == 0:
        return set(TEST_IMAGES)
    count = min(n, len(TEST_IMAGES))
    return set(TEST_IMAGES[:count])


# Test files that require serial execution — deselected when ``-n`` > 0.
_SERIAL_ONLY_FILES = {'test_lint.py', 'test_devcontainer_cli.py'}


def pytest_collection_modifyitems(config, items):
    """Apply automatic test deselection based on ``-n`` worker count.

    * ``-n0`` (default): full suite — all images, lint, devcontainer.
    * ``-n1``: smoke — alpine only, no lint/devcontainer.
    * ``-n2``: alpine + ubuntu, no lint/devcontainer.
    * ``-n3``: all images in parallel, no lint/devcontainer.
    """
    n = _numprocesses(config)
    deselected = []

    # --- Deselect serial-only files when running with workers ---
    if n > 0:
        remaining = []
        for item in items:
            if any(name in item.nodeid for name in _SERIAL_ONLY_FILES):
                deselected.append(item)
            else:
                remaining.append(item)
        items[:] = remaining

    # --- Deselect excluded images ---
    excluded_images = set(TEST_IMAGES) - _allowed_images(config)
    if excluded_images:
        remaining = []
        for item in items:
            if any(img in item.nodeid for img in excluded_images):
                deselected.append(item)
            else:
                remaining.append(item)
        items[:] = remaining

    if deselected:
        config.hook.pytest_deselected(items=deselected)


@pytest.fixture(autouse=True)
def _patch_module_constants(request, monkeypatch, tmp_path):
    """Pin module-level constants to deterministic fake values.

    Skipped for tests marked with ``@pytest.mark.live`` so that real
    host identity is used.
    """
    if {'live', 'devcontainer'} & {m.name for m in request.node.iter_markers()}:
        return
    podrun_tmp = tmp_path / 'podrun_tmp'
    podrun_tmp.mkdir()
    podrun_stores = tmp_path / 'podrun_stores'
    podrun_stores.mkdir()
    monkeypatch.setattr(podrun_mod, 'UID', FAKE_UID)
    monkeypatch.setattr(podrun_mod, 'GID', FAKE_GID)
    monkeypatch.setattr(podrun_mod, 'UNAME', FAKE_UNAME)
    monkeypatch.setattr(podrun_mod, 'USER_HOME', FAKE_USER_HOME)
    monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(podrun_tmp))
    monkeypatch.setattr(podrun_mod, '_PODRUN_STORES_DIR', str(podrun_stores))
    _real_which = shutil.which
    monkeypatch.setattr(
        podrun_mod.shutil,
        'which',
        lambda x: 'podman' if x == 'podman' else _real_which(x),
    )


@pytest.fixture
def podrun_tmp(tmp_path):
    """Return the monkeypatched PODRUN_TMP path for explicit assertions."""
    return tmp_path / 'podrun_tmp'


@pytest.fixture
def make_cli_args():
    """Factory returning argparse.Namespace with all fields merge_config reads."""

    def _factory(**overrides):
        defaults = dict(
            name=None,
            user_overlay=None,
            host_overlay=None,
            interactive_overlay=None,
            workspace=None,
            adhoc=None,
            x11=None,
            dood=None,
            shell=None,
            login=None,
            prompt_banner=None,
            auto_attach=None,
            auto_replace=None,
            print_cmd=False,
            print_overlays=False,
            config=None,
            no_devconfig=False,
            passthrough_args=[],
            trailing_args=[],
            explicit_command=[],
            export=None,
            fuse_overlayfs=None,
            had_config_script=False,
        )
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    return _factory


@pytest.fixture
def make_config():
    """Factory returning Config instances with overrides via dataclasses.replace."""

    def _factory(**overrides):
        base = Config(image='test-image:latest')
        return dataclasses.replace(base, **overrides)

    return _factory


class RunOsCmdController:
    """Controller for mocked run_os_cmd calls."""

    def __init__(self):
        self.calls = []
        self._return_value = None
        self._side_effect = None

    def set_return(self, stdout='', stderr='', returncode=0):
        import subprocess

        self._return_value = subprocess.CompletedProcess(
            args='',
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )
        self._side_effect = None

    def set_side_effect(self, effects):
        self._side_effect = list(effects)
        self._return_value = None

    def __call__(self, cmd):
        import subprocess

        self.calls.append(cmd)
        if self._side_effect is not None:
            if self._side_effect:
                val = self._side_effect.pop(0)
            else:
                val = subprocess.CompletedProcess(
                    args='',
                    returncode=0,
                    stdout='',
                    stderr='',
                )
            if isinstance(val, subprocess.CompletedProcess):
                return val
            raise val
        if self._return_value is not None:
            return self._return_value
        return subprocess.CompletedProcess(
            args='',
            returncode=0,
            stdout='',
            stderr='',
        )


@pytest.fixture
def mock_run_os_cmd(monkeypatch):
    """Monkeypatch run_os_cmd and return a controller."""
    ctrl = RunOsCmdController()
    monkeypatch.setattr(podrun_mod, 'run_os_cmd', ctrl)
    return ctrl


# ---------------------------------------------------------------------------
# Live integration test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope='session')
def podman_store(request, tmp_path_factory, worker_id):  # noqa: C901 — sequential store bootstrap; splitting would obscure the phase 1→2 flow
    """Per-worker podman store, bootstrapped from a shared base.

    Phase 1 — shared store init + image pull (serialized via filelock):
        Runs ``podrun store init`` and eagerly pulls every image in
        ``TEST_IMAGES`` into the shared ``.podrun-store/graphroot``.

    Phase 2 — per-worker store via ``podman image save | load``:
        Each xdist worker gets its own ``graphroot-<worker_id>`` populated
        by piping ``podman save`` from the shared store into
        ``podman load`` on the worker store.  This is a local copy (no
        network access) that correctly handles rootless UID-mapped
        overlay layers.  Each worker gets complete podman isolation
        (separate event log, container metadata) eliminating the
        event-tracking races that cause flakes with a shared store.

    In serial mode (``-n0``, worker_id == 'master') the shared store is
    used directly — no copy needed.
    """
    init_args = ['store', 'init', '--store-dir', str(PODRUN_STORE)]
    registry = request.config.getoption('--registry', default=None) or os.environ.get(
        'PODRUN_TEST_REGISTRY', ''
    )
    if registry:
        init_args += ['--registry', registry]

    # --- Phase 1: shared store init + eager image pull (serialized) ---
    lock = tmp_path_factory.getbasetemp().parent / 'podrun-store.lock'
    shared_graphroot = PODRUN_STORE / 'graphroot'

    # Build env/flags for the shared store (deterministic, no lock needed).
    shared_env = os.environ.copy()
    registries_conf = PODRUN_STORE / 'registries.conf'
    if registries_conf.exists():
        shared_env['CONTAINERS_REGISTRIES_CONF'] = str(registries_conf)

    shared_runroot = _runroot_path(str(shared_graphroot))
    shared_flags = [
        '--root',
        str(shared_graphroot),
        '--runroot',
        shared_runroot,
        '--storage-driver',
        'overlay',
    ]

    # Only pull/copy images that will actually be tested.
    images = sorted(_allowed_images(request.config))

    with filelock.FileLock(str(lock)):
        if not shared_graphroot.exists():
            podrun_main(init_args)

        # Eagerly pull test images.  The marker file records which images
        # have been pulled so incremental runs (e.g. going from
        # ``--test-images=1`` to ``--test-images=3``) only pull the new ones.
        pull_marker = PODRUN_STORE / '.images-pulled'
        already_pulled = set()
        if pull_marker.exists():
            already_pulled = set(pull_marker.read_text().strip().splitlines())
        to_pull = [img for img in images if img not in already_pulled]
        if to_pull:
            for image in to_pull:
                subprocess.run(
                    ['podman'] + shared_flags + ['pull', image],
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=True,
                    env=shared_env,
                )
            pull_marker.write_text('\n'.join(sorted(already_pulled | set(to_pull))) + '\n')

    # --- Phase 2: per-worker store via save/load ---
    # Each worker gets its own graphroot populated by piping
    # ``podman save`` from the shared store into ``podman load``.
    # Workers can safely read from the shared store concurrently
    # (images are immutable after Phase 1), so no shared lock is needed.
    if worker_id == 'master':
        graphroot = str(shared_graphroot)
    else:
        worker_graphroot = PODRUN_STORE / f'graphroot-{worker_id}'
        if not worker_graphroot.exists():
            worker_graphroot.mkdir(parents=True)
            worker_runroot = _runroot_path(str(worker_graphroot))
            worker_flags = [
                '--root',
                str(worker_graphroot),
                '--runroot',
                worker_runroot,
                '--storage-driver',
                'overlay',
            ]
            try:
                for image in images:
                    save = subprocess.Popen(
                        ['podman'] + shared_flags + ['image', 'save', image],
                        stdout=subprocess.PIPE,
                        env=shared_env,
                    )
                    try:
                        subprocess.run(
                            ['podman'] + worker_flags + ['image', 'load'],
                            stdin=save.stdout,
                            capture_output=True,
                            timeout=300,
                            check=True,
                            env=shared_env,
                        )
                    finally:
                        save.stdout.close()
                    save.wait(timeout=60)
            except Exception:
                subprocess.run(
                    ['podman', 'unshare', 'rm', '-rf', str(worker_graphroot)],
                    capture_output=True,
                    timeout=60,
                )
                raise
        graphroot = str(worker_graphroot)

    runroot = _runroot_path(graphroot)
    store = {
        'root': graphroot,
        'runroot': runroot,
        'storage_driver': 'overlay',
    }
    yield store

    # --- Teardown: clean up per-worker store and its runroot ---
    if worker_id == 'master':
        return
    try:
        subprocess.run(
            ['podman', 'unshare', 'rm', '-rf', graphroot],
            capture_output=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass
    if os.path.exists(runroot):
        try:
            shutil.rmtree(runroot)
        except OSError:
            pass


@pytest.fixture(scope='session')
def podman_store_flags(podman_store):
    """Return podman global flags for project-local storage.

    These flags bypass all config resolution and work correctly for
    rootless podman.
    """
    return [
        '--root',
        podman_store['root'],
        '--runroot',
        podman_store['runroot'],
        '--storage-driver',
        podman_store['storage_driver'],
    ]


@pytest.fixture(scope='session')
def podman_env(podman_store):
    """Return env dict for project-local podman.

    Storage paths are passed via CLI flags (``--root``, ``--runroot``,
    etc.).  Only ``CONTAINERS_REGISTRIES_CONF`` is set here when a
    registry mirror was configured during ``store init``.
    """
    env = os.environ.copy()
    registries_conf = PODRUN_STORE / 'registries.conf'
    if registries_conf.exists():
        env['CONTAINERS_REGISTRIES_CONF'] = str(registries_conf)
    return env


@pytest.fixture
def podman_run(podman_store_flags, podman_env):
    """Factory that wraps ``subprocess.run`` with project-local storage flags.

    Usage::

        result = podman_run(['run', '--rm', 'alpine', 'echo', 'hi'])
    """

    def _run(args, **kwargs):
        cmd = ['podman'] + podman_store_flags + args
        kwargs.setdefault('capture_output', True)
        kwargs.setdefault('text', True)
        kwargs.setdefault('timeout', 120)
        kwargs.setdefault('env', podman_env)
        return subprocess.run(cmd, **kwargs)

    return _run


@pytest.fixture
def container_name(worker_id):
    """Factory returning worker-unique container names for parallel safety."""

    def _make(base: str) -> str:
        if worker_id == 'master':
            return base
        return f'{base}-{worker_id}'

    return _make


@pytest.fixture(autouse=True)
def _podman_cleanup(request, podman_store_flags, podman_env):
    """After each live test, remove all containers from this worker's store.

    Each xdist worker has its own isolated podman store, so we can safely
    remove everything without affecting other workers.
    """
    yield
    if not ({'live', 'devcontainer'} & {m.name for m in request.node.iter_markers()}):
        return
    try:
        result = subprocess.run(
            ['podman'] + podman_store_flags + ['ps', '-a', '--format={{.Names}}'],
            capture_output=True,
            text=True,
            timeout=30,
            env=podman_env,
        )
        for name in result.stdout.strip().splitlines():
            name = name.strip()
            if not name:
                continue
            subprocess.run(
                ['podman'] + podman_store_flags + ['rm', '-f', '-t', '0', name],
                capture_output=True,
                timeout=30,
                env=podman_env,
            )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


@pytest.fixture(scope='session')
def has_userns():
    """Return True if ``--userns=keep-id`` works in this environment.

    Without subordinate UID/GID ranges (``/etc/subuid``, ``/etc/subgid``),
    user namespace mapping is unavailable and tests that depend on it must
    adapt their assertions accordingly.
    """
    try:
        import getpass

        with open('/etc/subuid') as f:
            return getpass.getuser() in f.read()
    except FileNotFoundError:
        return False


@pytest.fixture(scope='session')
def pull_image(podman_store_flags, podman_env):
    """Verify an image is present in the worker's store, pulling if needed.

    All ``TEST_IMAGES`` are pre-populated by ``podman_store`` (copied from
    the shared store), so this is typically a fast ``image exists`` check.
    Images not in ``TEST_IMAGES`` (e.g. custom-built) fall back to pull.
    """
    _verified = set()

    def _pull(image):
        if image in _verified:
            return image
        check = subprocess.run(
            ['podman'] + podman_store_flags + ['image', 'exists', image],
            capture_output=True,
            timeout=10,
            env=podman_env,
        )
        if check.returncode == 0:
            _verified.add(image)
            return image
        # Image not pre-populated; pull from registry.
        subprocess.run(
            ['podman'] + podman_store_flags + ['pull', image],
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
            env=podman_env,
        )
        _verified.add(image)
        return image

    return _pull


def _patch_entrypoint_for_no_userns(entrypoint_path):
    """Patch entrypoint for environments without user namespace support.

    When ``--userns=keep-id`` is unavailable (no subuid/subgid), the
    container runs as UID 0.  This patch:

    * Makes ``chown`` non-fatal (host UID doesn't map in the namespace).
    * Replaces the capsh/setpriv capability-drop with a direct exec,
      since cap-drop tools may be incompatible (e.g. busybox setpriv)
      and capability semantics differ when running as root.
    """
    with open(entrypoint_path, 'r') as f:
        content = f.read()

    import re

    # 1) Make chown non-fatal
    content = re.sub(
        r'^(\s*chown\b.*)$',
        r'\1 || true',
        content,
        flags=re.MULTILINE,
    )
    # Avoid double || true
    content = content.replace('|| true || true', '|| true')

    # 2) Replace the entire cap-drop block with a direct exec.
    #    The block starts at "# Drop bootstrap capabilities" and goes to EOF.
    cap_drop_pattern = re.compile(
        r'^(\s*# Drop bootstrap capabilities.*)',
        re.MULTILINE | re.DOTALL,
    )
    replacement = (
        '# Cap-drop bypassed (no userns support in test env)\n'
        'if [ $# -eq 0 ]; then\n'
        '  exec $SHELL\n'
        'else\n'
        '  exec "$@"\n'
        'fi\n'
    )
    content = cap_drop_pattern.sub(replacement, content)

    with open(entrypoint_path, 'w') as f:
        f.write(content)


_userns_supported = None  # cached result of userns probe


def _check_userns_support():
    """Return True if ``--userns=keep-id`` works in this environment."""
    global _userns_supported
    if _userns_supported is not None:
        return _userns_supported
    try:
        import getpass

        with open('/etc/subuid') as f:
            _userns_supported = getpass.getuser() in f.read()
    except FileNotFoundError:
        _userns_supported = False
    return _userns_supported


def run_podrun_live(podrun_args, podman_env, timeout=60, podman_store_flags=None, name_suffix=''):
    """Get podman command from ``podrun --print-cmd``, execute with local storage.

    This is a helper (not a fixture) used directly in live tests.

    When *podman_store_flags* is provided, ``--root``/``--runroot``/
    ``--storage-driver`` are passed to the podrun invocation so podman
    uses project-local storage via CLI flags (no ``XDG_CONFIG_HOME``).

    When ``--userns=keep-id`` is not supported by the environment (no
    subuid/subgid), the flag is stripped and the entrypoint is patched
    so that chown failures don't abort the script.

    *name_suffix* is appended to the auto-derived ``--name`` in the
    podman command for parallel worker isolation.
    """
    # Step 1: Get the podman command podrun would execute
    store_flags = podman_store_flags or []
    cmd = (
        [sys.executable, '-m', 'podrun']
        + store_flags
        + ['run', '--no-devconfig', '--print-cmd']
        + podrun_args
    )
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(PROJECT_ROOT),
        env=podman_env,
    )
    assert result.returncode == 0, f'print-cmd failed: {result.stderr}'

    # Step 2: Parse the command
    podman_cmd = shlex.split(result.stdout.strip())

    # Step 2b: Append worker suffix to --name for parallel isolation
    if name_suffix:
        podman_cmd = [f'{a}{name_suffix}' if a.startswith('--name=') else a for a in podman_cmd]

    # Step 3: Strip --userns=keep-id if environment doesn't support it
    strip_keepid = '--userns=keep-id' in podman_cmd and not _check_userns_support()
    if strip_keepid:
        podman_cmd = [a for a in podman_cmd if a != '--userns=keep-id']
        # Patch entrypoint on disk to make chown non-fatal
        for arg in podman_cmd:
            if arg.startswith('-v=') and 'entrypoint' in arg and ':' in arg:
                host_path = arg.split('=', 1)[1].split(':')[0]
                _patch_entrypoint_for_no_userns(host_path)
                break

    # Step 4: Execute with store flags in env
    return subprocess.run(
        podman_cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=podman_env,
    )


# ---------------------------------------------------------------------------
# Devcontainer CLI integration test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope='session')
def devcontainer_bin():
    """Find devcontainer CLI binary. Skip if not available."""
    path = shutil.which('devcontainer')
    if path is None:
        pytest.skip('devcontainer CLI not available')
    return path


@pytest.fixture(scope='session')
def podrun_wrapper(tmp_path_factory, podman_store_flags):
    """Create a temporary executable wrapper script for podrun.

    Needed because ``--docker-path`` requires an executable file path
    and ``podrun`` may not be installed as a console script.  Includes
    project-local storage flags so the devcontainer CLI uses the same
    store (and registry mirror) as the rest of the test suite.
    """
    flags = ' '.join(f'"{f}"' for f in podman_store_flags)
    tmpdir = tmp_path_factory.mktemp('podrun_wrapper')
    wrapper = tmpdir / 'podrun'
    wrapper.write_text(f'#!/bin/bash\nexec "{sys.executable}" -m podrun {flags} "$@"\n')
    wrapper.chmod(0o755)
    return str(wrapper)
