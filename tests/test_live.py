"""Live container integration tests for podrun.

These tests launch real containers with podman to validate the
entrypoint/rc.sh user-setup machinery end-to-end.  All container
storage is kept project-local under ``.podrun-store/`` so there is no
interference with the user's system podman.

Run selectively::

    pytest tests/test_live.py -v
"""

import getpass
import os
import platform
import shlex
import shutil
import subprocess
import sys

import pytest

from conftest import PROJECT_ROOT, run_podrun_live

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(shutil.which('podman') is None, reason='podman not available'),
]

# Current host identity – used for assertions inside containers.
HOST_UID = os.getuid()
HOST_GID = os.getgid()
HOST_USER = getpass.getuser()
HOST_HOSTNAME = platform.node()

# Distro matrix — alpine (busybox/ash), ubuntu (dash/bash/setpriv),
# fedora (bash/gawk/capsh).  Each image exercises different shell
# implementations and cap-drop tool paths in the entrypoint.
DISTRO_IMAGES = [
    'alpine:latest',
    'ubuntu:24.04',
    'fedora:latest',
]

# Images known to ship with bash pre-installed.
BASH_IMAGES = [
    'ubuntu:24.04',
    'fedora:latest',
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_live_rm(podrun_args, podman_env, timeout=60, podman_store_flags=None, name_suffix=''):
    """Run podrun live with ``--rm`` auto-injected as a passthrough flag."""
    return run_podrun_live(
        ['--rm'] + podrun_args,
        podman_env,
        timeout=timeout,
        podman_store_flags=podman_store_flags,
        name_suffix=name_suffix,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def live_name_suffix(worker_id):
    """Return a suffix for auto-derived container names in parallel mode."""
    if worker_id == 'master':
        return ''
    return f'-{worker_id}'


@pytest.fixture
def run_live_rm(podman_env, podman_store_flags, live_name_suffix):
    """Fixture-wrapped ``_run_live_rm`` with automatic worker-name isolation."""

    def _run(podrun_args, timeout=60):
        return _run_live_rm(
            podrun_args,
            podman_env,
            timeout=timeout,
            podman_store_flags=podman_store_flags,
            name_suffix=live_name_suffix,
        )

    return _run


@pytest.fixture(params=DISTRO_IMAGES, scope='module')
def distro_image(request, pull_image):
    """Pull and return each distro image (module-scoped parametrize)."""
    return pull_image(request.param)


@pytest.fixture(params=BASH_IMAGES, scope='module')
def bash_image(request, pull_image):
    """Pull and return each image that has bash pre-installed."""
    return pull_image(request.param)


@pytest.fixture
def live_store(tmp_path, podman_env):
    """Create a temporary store and destroy it (including runroot) on teardown."""

    def _init(extra_args=None, name='test-store'):
        sd = tmp_path / name
        result = subprocess.run(
            [sys.executable, '-m', 'podrun', 'store', 'init', '--store-dir', str(sd)]
            + (extra_args or []),
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(PROJECT_ROOT),
            env=podman_env,
        )
        assert result.returncode == 0, f'store init failed: {result.stderr}'
        _stores.append(sd)
        return sd

    _stores = []
    yield _init

    # Destroy all stores created during the test
    for sd in _stores:
        if sd.exists():
            subprocess.run(
                [sys.executable, '-m', 'podrun', 'store', 'destroy', '--store-dir', str(sd)],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(PROJECT_ROOT),
                env=podman_env,
            )


@pytest.fixture(scope='module')
def alpine_image(pull_image):
    return pull_image('alpine:latest')


@pytest.fixture(scope='module')
def ubuntu_image(pull_image):
    return pull_image('ubuntu:24.04')


@pytest.fixture(scope='module')
def fedora_image(pull_image):
    return pull_image('fedora:latest')


# ---------------------------------------------------------------------------
# TestUserOverlayBasic — Core user identity mapping
# ---------------------------------------------------------------------------


class TestUserOverlayBasic:
    """Validate --user-overlay maps host identity into the container."""

    def test_user_exists_in_container(self, distro_image, has_userns, run_live_rm):
        result = run_live_rm(
            ['--user-overlay', distro_image, 'cat', '/etc/passwd'],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        # Entrypoint writes HOST_USER:x:HOST_UID:HOST_GID:... to /etc/passwd
        assert HOST_USER in result.stdout
        if has_userns:
            assert str(HOST_UID) in result.stdout

    def test_home_dir_created(self, distro_image, has_userns, run_live_rm):
        result = run_live_rm(
            [
                '--user-overlay',
                distro_image,
                'sh',
                '-c',
                f'test -d /home/{HOST_USER} && echo exists',
            ],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert 'exists' in result.stdout

    def test_workdir_symlink(self, distro_image, run_live_rm):
        result = run_live_rm(
            ['--host-overlay', distro_image, 'readlink', f'/home/{HOST_USER}/workdir'],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert result.stdout.strip() != ''


# ---------------------------------------------------------------------------
# TestShellDetection — Shell resolution across distros
# ---------------------------------------------------------------------------


class TestShellDetection:
    """Validate shell detection logic in entrypoint.sh."""

    def test_default_shell_bash(self, bash_image, run_live_rm):
        result = run_live_rm(
            ['--user-overlay', bash_image, 'sh', '-c', 'echo $SHELL'],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        shell = result.stdout.strip()
        assert shell.endswith('/bash'), f'Expected bash, got: {shell}'

    def test_default_shell_fallback_sh(self, alpine_image, run_live_rm):
        result = run_live_rm(
            ['--user-overlay', alpine_image, 'sh', '-c', 'echo $SHELL'],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        shell = result.stdout.strip()
        # Alpine has no bash by default; should fall back to sh
        assert shell.endswith('/sh') or shell.endswith('/bash'), f'Unexpected: {shell}'

    def test_explicit_shell_override(self, bash_image, run_live_rm):
        result = run_live_rm(
            ['--user-overlay', '--shell=sh', bash_image, 'sh', '-c', 'echo $SHELL'],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        shell = result.stdout.strip()
        assert shell.endswith('/sh'), f'Expected sh, got: {shell}'

    def test_shell_not_found_fallback(self, alpine_image, run_live_rm):
        """When --shell names a missing binary, fall back to sh with warning."""
        result = run_live_rm(
            ['--user-overlay', '--shell=zsh', alpine_image, 'sh', '-c', 'echo $SHELL'],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        shell = result.stdout.strip()
        # zsh is not installed on alpine — should fall back to /bin/sh
        assert shell.endswith('/sh'), f'Expected sh fallback, got: {shell}'
        # The entrypoint emits a warning on stderr
        assert 'zsh not found' in result.stderr or 'not found' in result.stderr, (
            f'Expected fallback warning in stderr: {result.stderr}'
        )


# ---------------------------------------------------------------------------
# TestEntrypointExec — Entrypoint mechanics
# ---------------------------------------------------------------------------


class TestEntrypointExec:
    """Validate entrypoint executes commands correctly."""

    def test_entrypoint_runs_command(self, distro_image, run_live_rm):
        result = run_live_rm(
            ['--user-overlay', distro_image, 'echo', 'hello'],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert 'hello' in result.stdout

    def test_entrypoint_exits_cleanly(self, distro_image, run_live_rm):
        result = run_live_rm(
            ['--user-overlay', distro_image, 'true'],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'

    def test_env_vars_propagated(self, distro_image, run_live_rm):
        result = run_live_rm(
            ['--user-overlay', '-e', 'FOO=bar', distro_image, 'sh', '-c', 'echo $FOO'],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert 'bar' in result.stdout


# ---------------------------------------------------------------------------
# TestHostOverlay — Host system context
# ---------------------------------------------------------------------------


class TestHostOverlay:
    """Validate --host-overlay maps host context into the container."""

    def test_workspace_mount(self, distro_image, podman_env, tmp_path, podman_store_flags):
        # Create a sentinel file on host
        sentinel = tmp_path / 'sentinel.txt'
        sentinel.write_text('live-test-marker')

        # Mount tmp_path into the container as workspace
        result = run_podrun_live(
            [
                '--host-overlay',
                '--rm',
                f'-v={tmp_path}:/test-workspace:ro',
                distro_image,
                'cat',
                '/test-workspace/sentinel.txt',
            ],
            podman_env,
            podman_store_flags=podman_store_flags,
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert 'live-test-marker' in result.stdout

    def test_hostname_matches(self, distro_image, run_live_rm):
        # Use cat /etc/hostname — the `hostname` command may be absent in
        # minimal container images (e.g. fedora:latest).
        result = run_live_rm(
            ['--host-overlay', distro_image, 'sh', '-c', 'cat /etc/hostname'],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert result.stdout.strip() == HOST_HOSTNAME


# ---------------------------------------------------------------------------
# TestCapabilityDrop — Bootstrap capability lifecycle
# ---------------------------------------------------------------------------


class TestCapabilityDrop:
    """Validate bootstrap capabilities are dropped after entrypoint setup."""

    # Capability bit positions from capabilities.h.
    CAP_CHOWN = 0
    CAP_DAC_OVERRIDE = 1
    CAP_FOWNER = 3
    CAP_SETPCAP = 8

    BOOTSTRAP_BITS = (
        (1 << CAP_CHOWN) | (1 << CAP_DAC_OVERRIDE) | (1 << CAP_FOWNER) | (1 << CAP_SETPCAP)
    )

    @staticmethod
    def _parse_caps(proc_status_output):
        """Parse all Cap* fields from /proc/self/status into a dict."""
        caps = {}
        for line in proc_status_output.splitlines():
            for field in ('CapInh', 'CapPrm', 'CapEff', 'CapAmb'):
                if line.startswith(f'{field}:'):
                    caps[field] = int(line.split(':')[1].strip(), 16)
        return caps

    @staticmethod
    def _parse_cap_eff(proc_status_output):
        """Extract the CapEff hex value from /proc/self/status output."""
        for line in proc_status_output.splitlines():
            if line.startswith('CapEff:'):
                return int(line.split(':')[1].strip(), 16)
        return None

    @classmethod
    def _assert_bootstrap_caps_cleared(cls, caps):
        """Assert all bootstrap caps are zeroed across CapEff and CapAmb."""
        for field in ('CapEff', 'CapAmb'):
            assert field in caps, f'{field} not found in /proc/self/status'
            assert not (caps[field] & cls.BOOTSTRAP_BITS), (
                f'bootstrap caps remain in {field}: {caps[field]:#x}'
            )

    def test_caps_dropped(self, distro_image, has_userns, run_live_rm):
        """Bootstrap caps are cleared across all distros / cap-drop backends."""
        result = run_live_rm(
            ['--user-overlay', distro_image, 'cat', '/proc/self/status'],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        if has_userns:
            caps = self._parse_caps(result.stdout)
            self._assert_bootstrap_caps_cleared(caps)

    def test_user_cap_survives_dedup(self, distro_image, has_userns, run_live_rm):
        """User --cap-add=CAP_DAC_OVERRIDE survives the entrypoint drop."""
        result = run_live_rm(
            [
                '--user-overlay',
                '--cap-add=CAP_DAC_OVERRIDE',
                distro_image,
                'cat',
                '/proc/self/status',
            ],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        if has_userns:
            caps = self._parse_caps(result.stdout)
            # CAP_DAC_OVERRIDE was user-provided — should survive
            for field in ('CapEff', 'CapAmb'):
                assert caps[field] & (1 << self.CAP_DAC_OVERRIDE), (
                    f'CAP_DAC_OVERRIDE should survive in {field}'
                )
            # Other bootstrap caps should still be dropped
            other_bits = self.BOOTSTRAP_BITS & ~(1 << self.CAP_DAC_OVERRIDE)
            assert not (caps['CapEff'] & other_bits), (
                f'other bootstrap caps remain in CapEff: {caps["CapEff"]:#x}'
            )

    def test_passwd_written_before_cap_drop(self, distro_image, run_live_rm):
        """Verify /etc/passwd has the entry (proves caps were available during setup)."""
        result = run_live_rm(
            ['--user-overlay', distro_image, 'sh', '-c', f'grep {HOST_USER} /etc/passwd'],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert HOST_USER in result.stdout


# ---------------------------------------------------------------------------
# TestRcShBanner — RC shell sourcing
# ---------------------------------------------------------------------------


class TestRcShBanner:
    """Validate rc.sh is sourced and prompt/banner configuration works."""

    def test_rc_sh_sourced_in_bash(self, ubuntu_image, run_live_rm):
        """Verify that .bashrc sources rc.sh."""
        result = run_live_rm(
            ['--user-overlay', ubuntu_image, 'sh', '-c', f'cat /home/{HOST_USER}/.bashrc'],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert '/.podrun/rc.sh' in result.stdout

    def test_custom_prompt_banner(self, distro_image, run_live_rm):
        """Verify --prompt-banner text appears in the rc.sh script inside container."""
        result = run_live_rm(
            ['--user-overlay', '--prompt-banner=MYTEST', distro_image, 'cat', '/.podrun/rc.sh'],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert 'MYTEST' in result.stdout


# ---------------------------------------------------------------------------
# TestLoginFlag — Login shell mode
# ---------------------------------------------------------------------------


class TestLoginFlag:
    """Validate --login flag works across distros.

    The --login flag adds ``-l`` to the entrypoint shebang, making it a
    login shell.  This caused failures on distros with capsh (e.g. fedora)
    because ``capsh --drop`` requires CAP_SETPCAP.  The fix was to prefer
    setpriv over capsh.
    """

    def test_login_runs_command(self, distro_image, run_live_rm):
        """--login --user-overlay IMAGE echo hello → works across all distros."""
        result = run_live_rm(
            ['--user-overlay', '--login', distro_image, 'echo', 'login-ok'],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert 'login-ok' in result.stdout

    def test_login_user_setup(self, distro_image, run_live_rm):
        """--login still performs full user setup (passwd, home dir)."""
        result = run_live_rm(
            [
                '--user-overlay',
                '--login',
                distro_image,
                'sh',
                '-c',
                f'grep {HOST_USER} /etc/passwd && test -d /home/{HOST_USER}',
            ],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert HOST_USER in result.stdout

    def test_login_cap_drop(self, ubuntu_image, has_userns, run_live_rm):
        """--login still drops bootstrap caps (via setpriv)."""
        result = run_live_rm(
            ['--user-overlay', '--login', ubuntu_image, 'cat', '/proc/self/status'],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        if has_userns:
            caps = TestCapabilityDrop._parse_caps(result.stdout)
            TestCapabilityDrop._assert_bootstrap_caps_cleared(caps)

    def test_login_with_host_overlay(self, distro_image, run_live_rm):
        """--login + --host-overlay → hostname and workspace mount work."""
        result = run_live_rm(
            ['--host-overlay', '--login', distro_image, 'sh', '-c', 'cat /etc/hostname'],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert result.stdout.strip() == HOST_HOSTNAME


# ---------------------------------------------------------------------------
# TestContainerLifecycle — Named container management
# ---------------------------------------------------------------------------


class TestContainerLifecycle:
    """Test named container run, attach, and replace flows."""

    def test_named_container_run_and_exec(self, distro_image, podman_run, container_name):
        """Run named container in background, exec into it, verify both succeed."""
        name = container_name('podrun-test-lifecycle-run')
        # Start a container that sleeps (no --userns=keep-id for compatibility)
        podman_run(
            [
                'run',
                '-d',
                '--name',
                name,
                distro_image,
                'sleep',
                '30',
            ]
        )
        try:
            # Exec into the running container
            result = podman_run(['exec', name, 'echo', 'inside'])
            assert result.returncode == 0, f'stderr: {result.stderr}'
            assert 'inside' in result.stdout
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_named_container_replace(self, distro_image, podman_run, container_name):
        """Run named container, then replace it, verify new container works."""
        name = container_name('podrun-test-lifecycle-replace')
        # Start first container
        podman_run(
            [
                'run',
                '-d',
                '--name',
                name,
                distro_image,
                'sleep',
                '30',
            ]
        )
        try:
            # Remove and replace
            podman_run(['rm', '-f', '-t', '0', name])
            r2 = podman_run(
                [
                    'run',
                    '-d',
                    '--name',
                    name,
                    distro_image,
                    'sleep',
                    '30',
                ]
            )
            assert r2.returncode == 0, f'stderr: {r2.stderr}'

            result = podman_run(['exec', name, 'echo', 'replaced'])
            assert result.returncode == 0, f'stderr: {result.stderr}'
            assert 'replaced' in result.stdout
        finally:
            podman_run(['rm', '-f', '-t', '0', name])


# ---------------------------------------------------------------------------
# Helpers — run podrun directly (not via --print-cmd)
# ---------------------------------------------------------------------------


def _run_podrun(podrun_args, podman_env, podman_store_flags=None, timeout=30):
    """Run ``podrun run`` directly (not via ``--print-cmd``).

    Unlike ``run_podrun_live`` which uses ``--print-cmd`` to get a podman
    command and then executes it, this helper runs ``podrun`` as a normal
    subprocess.  This is needed for testing auto-attach/auto-replace flows
    where podrun itself calls ``os.execvpe`` (replaced by subprocess boundary).
    """
    store_flags = podman_store_flags or []
    cmd = [sys.executable, '-m', 'podrun'] + store_flags + ['run', '--no-devconfig'] + podrun_args
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(PROJECT_ROOT),
        env=podman_env,
    )


# ---------------------------------------------------------------------------
# TestAutoAttach — auto-attach / auto-replace with overlay guard
# ---------------------------------------------------------------------------


class TestAutoAttach:
    """Validate auto-attach and auto-replace lifecycle.

    Tests the scenario from the user's bug report:

    1. ``podrun run --auto-attach IMAGE`` with no overlays creates a bare
       container that exits immediately (alpine /bin/sh with no tty).
    2. ``podrun run --adhoc --auto-attach IMAGE`` tries to attach to that
       stopped container but should fail because exec-entrypoint.sh is not
       present (the container was not created with user overlay).
    """

    def test_auto_attach_rejects_non_overlay_container(
        self, distro_image, podman_run, podman_env, podman_store_flags, container_name
    ):
        """auto-attach to a stopped container created without overlay → warns, exits 0.

        Reproduces: podrun run --auto-attach alpine → exits →
                    podrun run --auto-attach alpine → can't attach (non-running).
        """
        name = container_name('podrun-test-autoattach-nooverlay')
        # Step 1: Create a bare container (no overlay) via podrun
        _run_podrun(
            [f'--name={name}', distro_image, 'true'],
            podman_env,
            podman_store_flags=podman_store_flags,
        )
        try:
            # Container is now in Exited state. auto-attach warns (non-running).
            result = _run_podrun(
                ['--auto-attach', f'--name={name}', distro_image],
                podman_env,
                podman_store_flags=podman_store_flags,
            )
            assert result.returncode == 0
            assert 'Cannot auto-attach' in result.stderr
            assert 'non-running' in result.stderr
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_auto_attach_rejects_running_non_overlay_container(
        self, distro_image, podman_run, podman_env, podman_store_flags, container_name
    ):
        """auto-attach to a running container without overlay should also error."""
        name = container_name('podrun-test-autoattach-running-nooverlay')
        _run_podrun(
            [f'--name={name}', '-d', distro_image, 'sleep', '60'],
            podman_env,
            podman_store_flags=podman_store_flags,
        )
        try:
            result = _run_podrun(
                ['--auto-attach', f'--name={name}', distro_image],
                podman_env,
                podman_store_flags=podman_store_flags,
            )
            assert result.returncode != 0, f'Expected error, got: {result.stdout}'
            assert 'not created with podrun user overlay' in result.stderr
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_auto_attach_succeeds_with_overlay(
        self, distro_image, podman_run, podman_env, has_userns, podman_store_flags, container_name
    ):
        """auto-attach to a running container WITH overlay should succeed."""
        name = container_name('podrun-test-autoattach-overlay')
        # Create container through podrun with user overlay (detached, sleeping)
        _start_detached(distro_image, podman_env, podman_store_flags, name=name)
        try:
            # auto-attach with --print-cmd to capture the exec command
            result = _run_podrun(
                ['--auto-attach', '--user-overlay', f'--name={name}', '--print-cmd', distro_image],
                podman_env,
                podman_store_flags=podman_store_flags,
            )
            assert result.returncode == 0, f'stderr: {result.stderr}'
            assert 'exec' in result.stdout
            assert name in result.stdout
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_auto_replace_bypasses_overlay_guard(
        self, distro_image, podman_run, podman_env, podman_store_flags, container_name
    ):
        """auto-replace removes the old container and runs fresh — no guard needed."""
        name = container_name('podrun-test-autoreplace')
        # Create a bare container (no overlay) via podrun
        _run_podrun(
            [f'--name={name}', distro_image, 'true'],
            podman_env,
            podman_store_flags=podman_store_flags,
        )
        try:
            # auto-replace should remove the old container and print a new run cmd
            result = _run_podrun(
                ['--auto-replace', f'--name={name}', '--print-cmd', distro_image],
                podman_env,
                podman_store_flags=podman_store_flags,
            )
            assert result.returncode == 0, f'stderr: {result.stderr}'
            # Should be a 'run' command (not exec), since it replaced
            assert 'run' in result.stdout
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_adhoc_auto_attach_rejects_bare_container(
        self, distro_image, podman_run, podman_env, podman_store_flags, container_name
    ):
        """The exact user scenario: run bare → run --adhoc --auto-attach → warns, exits 0.

        ``podrun run --auto-attach alpine`` creates alpine-latest (no overlay).
        ``podrun run --adhoc --auto-attach alpine`` finds the stopped container
        and warns that it can't attach (non-running state).
        """
        name = container_name('podrun-test-adhoc-bare')
        # Step 1: Bare run (no overlay, exits immediately) via podrun
        _run_podrun(
            [f'--name={name}', distro_image, 'true'],
            podman_env,
            podman_store_flags=podman_store_flags,
        )
        try:
            # Step 2: --adhoc --auto-attach warns (non-running), exits 0
            result = _run_podrun(
                ['--adhoc', '--auto-attach', f'--name={name}', distro_image],
                podman_env,
                podman_store_flags=podman_store_flags,
            )
            assert result.returncode == 0
            assert 'Cannot auto-attach' in result.stderr
            assert 'non-running' in result.stderr
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_adhoc_auto_attach_works_after_overlay_run(
        self, distro_image, podman_run, podman_env, has_userns, podman_store_flags, container_name
    ):
        """--adhoc --auto-attach succeeds when the container has overlay."""
        name = container_name('podrun-test-adhoc-overlay')
        _start_detached(distro_image, podman_env, podman_store_flags, name=name)
        try:
            result = _run_podrun(
                ['--adhoc', '--auto-attach', f'--name={name}', '--print-cmd', distro_image],
                podman_env,
                podman_store_flags=podman_store_flags,
            )
            assert result.returncode == 0, f'stderr: {result.stderr}'
            assert 'exec' in result.stdout
        finally:
            podman_run(['rm', '-f', '-t', '0', name])


# ---------------------------------------------------------------------------
# TestPrintCmd — Sanity check that --print-cmd produces a runnable command
# ---------------------------------------------------------------------------


class TestSubcommandPassthrough:
    """Test podman subcommand passthrough works end-to-end."""

    def test_podrun_version(self):
        """podrun version should passthrough to podman version."""
        import subprocess as _sp

        result = _sp.run(
            [sys.executable, '-m', 'podrun', 'version'],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert 'podman' in result.stdout.lower() or 'version' in result.stdout.lower()

    def test_podrun_dash_v(self):
        """podrun -v should print both versions (devcontainer CLI isPodman check)."""
        import subprocess as _sp

        result = _sp.run(
            [sys.executable, '-m', 'podrun', '-v'],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert 'podrun' in result.stdout
        # Must contain 'podman' for devcontainer CLI isPodman detection
        assert 'podman' in result.stdout.lower()


class TestExecOverlay:
    """Test exec with overlay flags (env, workdir)."""

    def test_exec_with_env(self, distro_image, podman_run, container_name):
        """Exec into a running container with environment variable."""
        name = container_name('podrun-test-exec-env')
        podman_run(
            [
                'run',
                '-d',
                '--name',
                name,
                distro_image,
                'sleep',
                '30',
            ]
        )
        try:
            result = podman_run(
                [
                    'exec',
                    '-e=MYVAR=hello',
                    name,
                    'sh',
                    '-c',
                    'echo $MYVAR',
                ]
            )
            assert result.returncode == 0, f'stderr: {result.stderr}'
            assert 'hello' in result.stdout
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_exec_with_workdir(self, distro_image, podman_run, container_name):
        """Exec into a running container with workdir."""
        name = container_name('podrun-test-exec-workdir')
        podman_run(
            [
                'run',
                '-d',
                '--name',
                name,
                distro_image,
                'sleep',
                '30',
            ]
        )
        try:
            result = podman_run(
                [
                    'exec',
                    '-w=/tmp',
                    name,
                    'pwd',
                ]
            )
            assert result.returncode == 0, f'stderr: {result.stderr}'
            assert '/tmp' in result.stdout
        finally:
            podman_run(['rm', '-f', '-t', '0', name])


class TestPrintCmd:
    """Verify --print-cmd output is a valid, runnable podman command."""

    def test_print_cmd_is_valid_podman(self, distro_image, run_live_rm):
        result = run_live_rm(
            ['--user-overlay', distro_image, 'echo', 'print-cmd-ok'],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert 'print-cmd-ok' in result.stdout


# ---------------------------------------------------------------------------
# TestStoreInitLive — End-to-end store init with real podman
# ---------------------------------------------------------------------------


class TestStoreInitLive:
    """Verify ``podrun store init`` creates a working project-local store."""

    def test_store_init_podman_images(self, live_store, podman_env):
        """store init → activate → podman images succeeds with correct graphroot."""
        store_dir = live_store(name='live-store')
        assert (store_dir / 'activate').exists()

        # Source activate and run podman images
        result = subprocess.run(
            [
                'bash',
                '-c',
                f'source "{store_dir}/activate" && '
                f'podman images --format "{{{{.Repository}}}}" && '
                f'podman info --format "{{{{.Store.GraphRoot}}}}"',
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=podman_env,
        )
        assert result.returncode == 0, f'podman failed: {result.stderr}'
        lines = result.stdout.strip().splitlines()
        graphroot_line = lines[-1]
        assert str(store_dir / 'graphroot') == graphroot_line

    def test_store_init_which_podman(self, live_store, podman_env):
        """After activation, ``which podman`` resolves to the store wrapper."""
        store_dir = live_store(name='live-store')
        result = subprocess.run(
            ['bash', '-c', f'source "{store_dir}/activate" && which podman'],
            capture_output=True,
            text=True,
            timeout=10,
            env=podman_env,
        )
        assert result.returncode == 0
        assert str(store_dir / 'bin' / 'podman') == result.stdout.strip()

    def test_store_destroy_after_init(self, tmp_path, podman_env):
        """store destroy removes both store dir and runroot target."""
        store_dir = tmp_path / 'live-store'
        subprocess.run(
            [sys.executable, '-m', 'podrun', 'store', 'init', '--store-dir', str(store_dir)],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(PROJECT_ROOT),
            env=podman_env,
        )
        runroot_target = os.readlink(str(store_dir / 'runroot'))
        assert os.path.exists(runroot_target)

        result = subprocess.run(
            [sys.executable, '-m', 'podrun', 'store', 'destroy', '--store-dir', str(store_dir)],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(PROJECT_ROOT),
            env=podman_env,
        )
        assert result.returncode == 0, f'destroy failed: {result.stderr}'
        assert not store_dir.exists()
        assert not os.path.exists(runroot_target)


# ---------------------------------------------------------------------------
# TestExport — --export flag (reverse volume mounts)
# ---------------------------------------------------------------------------


@pytest.mark.live
class TestExport:
    """Validate --export populates host directories from container content."""

    def test_export_dir_populates_host(self, distro_image, tmp_path, run_live_rm):
        """Export /etc/profile.d → host dir; verify host dir has files."""
        host_dir = tmp_path / 'test-export-dir'
        result = run_live_rm(
            [
                '--user-overlay',
                '--export',
                f'/etc/profile.d:{host_dir}',
                distro_image,
                'ls',
                '/etc/profile.d',
            ],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert any(host_dir.iterdir()), (
            f'no files in {host_dir}: {list(host_dir.iterdir()) if host_dir.exists() else "dir missing"}'
        )

    def test_export_file_populates_host(self, distro_image, tmp_path, run_live_rm):
        """Export /etc/profile → host dir; verify file appears."""
        host_dir = tmp_path / 'test-export-file'
        result = run_live_rm(
            [
                '--user-overlay',
                '--export',
                f'/etc/profile:{host_dir}',
                distro_image,
                'cat',
                '/etc/profile',
            ],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert (host_dir / 'profile').exists(), (
            f'profile not found in {host_dir}: {list(host_dir.iterdir()) if host_dir.exists() else "dir missing"}'
        )

    def test_export_symlink_in_container(self, distro_image, tmp_path, run_live_rm):
        """Verify container path is a symlink to /.podrun/exports/<hash>."""
        import hashlib

        host_dir = tmp_path / 'test-export-symlink'
        staging_hash = hashlib.sha256('/var/log'.encode()).hexdigest()[:12]
        result = run_live_rm(
            [
                '--user-overlay',
                '--export',
                f'/var/log:{host_dir}',
                distro_image,
                'readlink',
                '/var/log',
            ],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert f'/.podrun/exports/{staging_hash}' == result.stdout.strip()

    def test_export_idempotent_skip(self, distro_image, tmp_path, run_live_rm):
        """Pre-populate host dir; verify container doesn't overwrite."""
        host_dir = tmp_path / 'test-export-idempotent'
        host_dir.mkdir()
        sentinel = host_dir / 'sentinel.txt'
        sentinel.write_text('host-side-content')
        result = run_live_rm(
            [
                '--user-overlay',
                '--export',
                f'/var/log:{host_dir}',
                distro_image,
                'cat',
                '/var/log/sentinel.txt',
            ],
        )
        # The container sees host content via bind mount + symlink
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert 'host-side-content' in result.stdout
        # Host sentinel file is preserved
        assert sentinel.read_text() == 'host-side-content'

    def test_export_copy_only(self, distro_image, tmp_path, run_live_rm):
        """Copy-only (:0) exports /etc without failing on bind mounts."""
        host_dir = tmp_path / 'test-export-copy'
        result = run_live_rm(
            [
                '--user-overlay',
                '--export',
                f'/etc:{host_dir}:0',
                distro_image,
                'test',
                '-d',
                '/etc',
            ],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        # Host dir should have /etc content (passwd, group, etc.)
        assert (host_dir / 'passwd').exists(), (
            f'passwd not in {host_dir}: {list(host_dir.iterdir()) if host_dir.exists() else "dir missing"}'
        )
        # /etc is still a real directory in the container (not a symlink)

    def test_export_nonexistent_creates_symlink(self, distro_image, tmp_path, run_live_rm):
        """Non-existent container path gets symlinked to staging in strict mode."""
        import hashlib

        host_dir = tmp_path / 'test-export-nonexistent'
        container_path = '/opt/nonexistent-test-export'
        staging_hash = hashlib.sha256(container_path.encode()).hexdigest()[:12]
        result = run_live_rm(
            [
                '--user-overlay',
                '--export',
                f'{container_path}:{host_dir}',
                distro_image,
                'sh',
                '-c',
                f'readlink {container_path} && echo created > {container_path}/proof.txt',
            ],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert f'/.podrun/exports/{staging_hash}' == result.stdout.strip().splitlines()[0]
        # File written through the symlink should appear on the host
        assert (host_dir / 'proof.txt').exists(), (
            f'proof.txt not on host: {list(host_dir.iterdir()) if host_dir.exists() else "dir missing"}'
        )

    def test_export_strict_fails_on_bind_mounts(self, distro_image, tmp_path, run_live_rm):
        """Strict export of /etc fails because bind-mounted files block rm."""
        host_dir = tmp_path / 'test-export-strict-fail'
        result = run_live_rm(
            ['--user-overlay', '--export', f'/etc:{host_dir}', distro_image, 'true'],
        )
        assert result.returncode != 0, 'strict export of /etc should fail due to bind-mounted files'


# ---------------------------------------------------------------------------
# TestPasswdEntry — --passwd-entry correctness
# ---------------------------------------------------------------------------


class TestPasswdEntry:
    """Validate --passwd-entry sets correct HOME and identity in /etc/passwd."""

    def test_passwd_home_is_user_home(self, distro_image, has_userns, run_live_rm):
        """HOME field in /etc/passwd should be /home/<user>, not image WORKDIR."""
        result = run_live_rm(
            [
                '--user-overlay',
                distro_image,
                'sh',
                '-c',
                f'grep "^{HOST_USER}:" /etc/passwd | cut -d: -f6',
            ],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        if has_userns:
            assert result.stdout.strip() == f'/home/{HOST_USER}'

    def test_home_env_var(self, distro_image, has_userns, run_live_rm):
        """$HOME env var should be /home/<user>."""
        result = run_live_rm(
            ['--user-overlay', distro_image, 'sh', '-c', 'echo $HOME'],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        if has_userns:
            assert result.stdout.strip() == f'/home/{HOST_USER}'

    def test_passwd_shell_patched(self, bash_image, has_userns, run_live_rm):
        """SHELL field in /etc/passwd should be the resolved shell (not /bin/sh)."""
        result = run_live_rm(
            [
                '--user-overlay',
                bash_image,
                'sh',
                '-c',
                f'grep "^{HOST_USER}:" /etc/passwd | cut -d: -f7',
            ],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        if has_userns:
            shell = result.stdout.strip()
            assert shell.endswith('/bash'), f'Expected bash in passwd SHELL field, got: {shell}'

    def test_group_entry_exists(self, distro_image, has_userns, run_live_rm):
        """Group entry for host GID should exist in /etc/group."""
        result = run_live_rm(
            [
                '--user-overlay',
                distro_image,
                'sh',
                '-c',
                f'grep ":{HOST_GID}:" /etc/group | cut -d: -f1',
            ],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        if has_userns:
            assert result.stdout.strip() != '', f'No group entry for GID {HOST_GID}'

    def test_sudo_setup(self, bash_image, has_userns, run_live_rm):
        """Passwordless sudo should be configured for the host user."""
        result = run_live_rm(
            [
                '--user-overlay',
                bash_image,
                'sh',
                '-c',
                'command -v sudo > /dev/null 2>&1 && sudo -n -u root echo sudo-ok || echo no-sudo',
            ],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        out = result.stdout.strip()
        assert out in ('sudo-ok', 'no-sudo'), f'Unexpected: {out}'


# ---------------------------------------------------------------------------
# TestExecEntrypoint — exec-entrypoint.sh behavior in live containers
# ---------------------------------------------------------------------------


def _start_detached(image, podman_env, podman_store_flags, name, extra_args=None):
    """Start a detached container with user overlay, return the container name.

    Uses ``--print-cmd`` to get the full podman command with all overlay
    flags, modifies it to detach with a sleep command, and returns the
    running container name.
    """
    store_flags = podman_store_flags or []
    cmd = (
        [sys.executable, '-m', 'podrun']
        + store_flags
        + [
            'run',
            '--no-devconfig',
            '--print-cmd',
            '--user-overlay',
            f'--name={name}',
        ]
        + (extra_args or [])
        + [image]
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
    podman_cmd = shlex.split(result.stdout.strip())

    # Insert -d after 'run' and append sleep command
    run_idx = podman_cmd.index('run')
    podman_cmd.insert(run_idx + 1, '-d')
    podman_cmd.extend(['sleep', '120'])

    start = subprocess.run(
        podman_cmd,
        capture_output=True,
        text=True,
        timeout=30,
        env=podman_env,
    )
    assert start.returncode == 0, f'detached run failed: {start.stderr}'
    return name


EXEC_EP = '/.podrun/exec-entrypoint.sh'


class TestExecEntrypoint:
    """Validate exec-entrypoint.sh provides consistent exec sessions.

    These tests start a container through ``run_podrun_live`` (with full user
    overlay) in detached mode, then ``podman exec`` with the bind-mounted
    ``exec-entrypoint.sh`` to verify session setup.
    """

    def test_exec_home_env(
        self, distro_image, podman_run, podman_env, has_userns, podman_store_flags, container_name
    ):
        """HOME env var is /home/<user> inside exec-entrypoint.sh session."""
        name = _start_detached(
            distro_image,
            podman_env,
            podman_store_flags,
            name=container_name('podrun-test-exec-home'),
        )
        try:
            result = podman_run(['exec', name, EXEC_EP, '', '', 'sh', '-c', 'echo $HOME'])
            assert result.returncode == 0, f'stderr: {result.stderr}'
            if has_userns:
                assert result.stdout.strip() == f'/home/{HOST_USER}'
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_exec_shell_env(
        self, distro_image, podman_run, podman_env, has_userns, podman_store_flags, container_name
    ):
        """SHELL env var is set inside exec-entrypoint.sh session."""
        name = _start_detached(
            distro_image,
            podman_env,
            podman_store_flags,
            name=container_name('podrun-test-exec-shell'),
        )
        try:
            result = podman_run(['exec', name, EXEC_EP, '', '', 'sh', '-c', 'echo $SHELL'])
            assert result.returncode == 0, f'stderr: {result.stderr}'
            if has_userns:
                assert result.stdout.strip() != '', 'SHELL should be set'
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_exec_shell_resolves_bash(
        self, bash_image, podman_run, podman_env, has_userns, podman_store_flags, container_name
    ):
        """On images with bash, exec-entrypoint.sh resolves SHELL to bash."""
        name = _start_detached(
            bash_image, podman_env, podman_store_flags, name=container_name('podrun-test-exec-bash')
        )
        try:
            result = podman_run(['exec', name, EXEC_EP, '', '', 'sh', '-c', 'echo $SHELL'])
            assert result.returncode == 0, f'stderr: {result.stderr}'
            if has_userns:
                assert result.stdout.strip().endswith('/bash'), (
                    f'Expected bash, got: {result.stdout.strip()}'
                )
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_exec_shell_override(
        self, bash_image, podman_run, podman_env, has_userns, podman_store_flags, container_name
    ):
        """Shell override via $1 arg selects the specified shell.

        Note: exec-entrypoint.sh promotes sh→bash when bash is available,
        so overriding with 'sh' on a bash image still resolves to bash.
        We pass '/bin/bash' (full path) to verify the arg pathway works.
        """
        name = _start_detached(
            bash_image,
            podman_env,
            podman_store_flags,
            name=container_name('podrun-test-exec-shell-override'),
        )
        try:
            result = podman_run(['exec', name, EXEC_EP, '/bin/bash', '', 'sh', '-c', 'echo $SHELL'])
            assert result.returncode == 0, f'stderr: {result.stderr}'
            if has_userns:
                shell = result.stdout.strip()
                assert 'bash' in shell, f'Expected bash from $1 override, got: {shell}'
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_exec_shell_override_via_env(
        self, bash_image, podman_run, podman_env, has_userns, podman_store_flags, container_name
    ):
        """Shell override via PODRUN_SHELL env var selects the specified shell."""
        name = _start_detached(
            bash_image,
            podman_env,
            podman_store_flags,
            name=container_name('podrun-test-exec-shell-env-override'),
        )
        try:
            result = podman_run(
                [
                    'exec',
                    '-e=PODRUN_SHELL=/bin/bash',
                    name,
                    EXEC_EP,
                    '',
                    '',
                    'sh',
                    '-c',
                    'echo $SHELL',
                ]
            )
            assert result.returncode == 0, f'stderr: {result.stderr}'
            if has_userns:
                shell = result.stdout.strip()
                assert 'bash' in shell, f'Expected bash from env override, got: {shell}'
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_exec_login_override(
        self, alpine_image, podman_run, podman_env, has_userns, podman_store_flags, container_name
    ):
        """Login override via $2 arg — verify the shell is invoked."""
        name = _start_detached(
            alpine_image,
            podman_env,
            podman_store_flags,
            name=container_name('podrun-test-exec-login'),
        )
        try:
            # Login mode: exec-entrypoint.sh sources profile; just verify it runs
            result = podman_run(['exec', name, EXEC_EP, '', '1', 'sh', '-c', 'echo login-ok'])
            assert result.returncode == 0, f'stderr: {result.stderr}'
            assert 'login-ok' in result.stdout
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_exec_login_override_via_env(
        self, alpine_image, podman_run, podman_env, has_userns, podman_store_flags, container_name
    ):
        """Login override via PODRUN_LOGIN env var."""
        name = _start_detached(
            alpine_image,
            podman_env,
            podman_store_flags,
            name=container_name('podrun-test-exec-login-env'),
        )
        try:
            result = podman_run(
                [
                    'exec',
                    '-e=PODRUN_LOGIN=1',
                    name,
                    EXEC_EP,
                    '',
                    '',
                    'sh',
                    '-c',
                    'echo login-env-ok',
                ]
            )
            assert result.returncode == 0, f'stderr: {result.stderr}'
            assert 'login-env-ok' in result.stdout
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_exec_command_passthrough(
        self, distro_image, podman_run, podman_env, has_userns, podman_store_flags, container_name
    ):
        """Command args after shift 2 are passed through to exec."""
        name = _start_detached(
            distro_image,
            podman_env,
            podman_store_flags,
            name=container_name('podrun-test-exec-passthrough'),
        )
        try:
            result = podman_run(['exec', name, EXEC_EP, '', '', 'echo', 'passthrough-ok'])
            assert result.returncode == 0, f'stderr: {result.stderr}'
            assert 'passthrough-ok' in result.stdout
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_exec_stty_resize(
        self, alpine_image, podman_run, podman_env, has_userns, podman_store_flags, container_name
    ):
        """PODRUN_STTY_INIT env var triggers stty resize without error."""
        name = _start_detached(
            alpine_image,
            podman_env,
            podman_store_flags,
            name=container_name('podrun-test-exec-stty'),
        )
        try:
            result = podman_run(
                [
                    'exec',
                    '-e=PODRUN_STTY_INIT=rows 24 cols 80',
                    name,
                    EXEC_EP,
                    '',
                    '',
                    'echo',
                    'stty-ok',
                ]
            )
            assert result.returncode == 0, f'stderr: {result.stderr}'
            assert 'stty-ok' in result.stdout
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_exec_runs_without_elevated_caps(
        self, distro_image, podman_run, podman_env, has_userns, podman_store_flags, container_name
    ):
        """Exec sessions run successfully and can read /proc/self/status.

        Note: ``podman exec`` starts a new process in the container namespace.
        The effective cap set in exec sessions is determined by the container's
        bounding set, not the entrypoint's post-drop effective set.  The
        entrypoint's cap drop protects the run-entrypoint process tree; exec
        sessions inherit the container-level cap bounding set.
        """
        name = _start_detached(
            distro_image,
            podman_env,
            podman_store_flags,
            name=container_name('podrun-test-exec-caps'),
        )
        try:
            result = podman_run(['exec', name, EXEC_EP, '', '', 'cat', '/proc/self/status'])
            assert result.returncode == 0, f'stderr: {result.stderr}'
            if has_userns:
                # Verify we can parse caps (process ran successfully)
                caps = TestCapabilityDrop._parse_caps(result.stdout)
                assert 'CapEff' in caps, 'CapEff not found'
        finally:
            podman_run(['rm', '-f', '-t', '0', name])


# ---------------------------------------------------------------------------
# TestExecRcShSourcing — ENV= sourcing in exec sessions
# ---------------------------------------------------------------------------


class TestExecRcShSourcing:
    """Verify rc.sh is sourced via ENV= in exec-entrypoint.sh sessions."""

    def test_exec_env_rc_sourced(
        self, distro_image, podman_run, podman_env, has_userns, podman_store_flags, container_name
    ):
        """ENV=/.podrun/rc.sh causes POSIX shells to source rc.sh on startup.

        We verify by checking that the ENV variable is set inside the exec
        session (exec-entrypoint.sh passes it through from the container env).
        """
        name = _start_detached(
            distro_image,
            podman_env,
            podman_store_flags,
            name=container_name('podrun-test-exec-rc-sourced'),
        )
        try:
            result = podman_run(
                [
                    'exec',
                    '-e=ENV=/.podrun/rc.sh',
                    name,
                    EXEC_EP,
                    '',
                    '',
                    'sh',
                    '-c',
                    'echo $ENV',
                ]
            )
            assert result.returncode == 0, f'stderr: {result.stderr}'
            assert '/.podrun/rc.sh' in result.stdout
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_rc_sh_exists_in_container(
        self, distro_image, podman_run, podman_env, has_userns, podman_store_flags, container_name
    ):
        """rc.sh is bind-mounted and accessible inside exec sessions."""
        name = _start_detached(
            distro_image,
            podman_env,
            podman_store_flags,
            name=container_name('podrun-test-exec-rc-exists'),
        )
        try:
            result = podman_run(['exec', name, EXEC_EP, '', '', 'test', '-f', '/.podrun/rc.sh'])
            assert result.returncode == 0, f'stderr: {result.stderr}'
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_rc_sh_has_podrun_marker(
        self, distro_image, podman_run, podman_env, has_userns, podman_store_flags, container_name
    ):
        """rc.sh contains the PODRUN ascii art marker."""
        name = _start_detached(
            distro_image,
            podman_env,
            podman_store_flags,
            name=container_name('podrun-test-exec-rc-marker'),
        )
        try:
            result = podman_run(['exec', name, EXEC_EP, '', '', 'cat', '/.podrun/rc.sh'])
            assert result.returncode == 0, f'stderr: {result.stderr}'
            assert 'PODRUN' in result.stdout
        finally:
            podman_run(['rm', '-f', '-t', '0', name])


# ---------------------------------------------------------------------------
# TestContainerLifecycleWithOverlay — Named container + overlay + exec cycle
# ---------------------------------------------------------------------------


class TestContainerLifecycleWithOverlay:
    """Test named container with full user overlay, then exec-entrypoint cycle."""

    def test_overlay_run_then_exec(
        self, distro_image, podman_run, podman_env, has_userns, podman_store_flags, container_name
    ):
        """Start with user overlay, exec into it, verify identity persists."""
        name = _start_detached(
            distro_image,
            podman_env,
            podman_store_flags,
            name=container_name('podrun-test-lifecycle-overlay'),
        )
        try:
            # Verify user exists via exec-entrypoint
            result = podman_run(['exec', name, EXEC_EP, '', '', 'sh', '-c', 'whoami'])
            assert result.returncode == 0, f'stderr: {result.stderr}'
            if has_userns:
                assert HOST_USER in result.stdout
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_overlay_multiple_execs(
        self, distro_image, podman_run, podman_env, has_userns, podman_store_flags, container_name
    ):
        """Multiple exec sessions into same container all see correct state."""
        name = _start_detached(
            distro_image,
            podman_env,
            podman_store_flags,
            name=container_name('podrun-test-lifecycle-multi'),
        )
        try:
            # First exec: create a file in home
            podman_run(
                ['exec', name, EXEC_EP, '', '', 'sh', '-c', f'touch /home/{HOST_USER}/marker']
            )
            # Second exec: verify file persists
            result = podman_run(
                [
                    'exec',
                    name,
                    EXEC_EP,
                    '',
                    '',
                    'sh',
                    '-c',
                    f'test -f /home/{HOST_USER}/marker && echo persists',
                ]
            )
            assert result.returncode == 0, f'stderr: {result.stderr}'
            if has_userns:
                assert 'persists' in result.stdout
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_overlay_exec_inherits_env(
        self, distro_image, podman_run, podman_env, has_userns, podman_store_flags, container_name
    ):
        """Exec sessions inherit PODRUN_* env vars from the container."""
        name = _start_detached(
            distro_image,
            podman_env,
            podman_store_flags,
            name=container_name('podrun-test-lifecycle-env'),
        )
        try:
            result = podman_run(
                ['exec', name, EXEC_EP, '', '', 'sh', '-c', 'echo $PODRUN_OVERLAYS']
            )
            assert result.returncode == 0, f'stderr: {result.stderr}'
            assert 'user' in result.stdout
        finally:
            podman_run(['rm', '-f', '-t', '0', name])


# ---------------------------------------------------------------------------
# TestWorkdirImage — Images with non-default WORKDIR
# ---------------------------------------------------------------------------


class TestWorkdirImage:
    """Verify HOME is correct even when the image has a non-default WORKDIR.

    Without ``--passwd-entry``, podman uses WORKDIR as the HOME directory
    in the auto-generated ``/etc/passwd`` entry.  Our ``--passwd-entry``
    flag forces HOME to ``/home/<user>`` regardless of image WORKDIR.
    """

    @pytest.fixture(scope='class')
    def workdir_image(self, podman_store_flags, podman_env):
        """Build a test image with WORKDIR=/app."""
        store_flags = podman_store_flags or []
        tag = 'podrun-test-workdir:latest'
        containerfile = 'FROM alpine:latest\nWORKDIR /app\n'
        cmd = ['podman'] + store_flags + ['build', '-t', tag, '-f', '-', '.']
        result = subprocess.run(
            cmd,
            input=containerfile,
            capture_output=True,
            text=True,
            timeout=120,
            env=podman_env,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0, f'build failed: {result.stderr}'
        return tag

    def test_home_not_workdir(self, workdir_image, has_userns, run_live_rm):
        """HOME should be /home/<user>, not /app (the image WORKDIR)."""
        result = run_live_rm(
            ['--user-overlay', workdir_image, 'sh', '-c', 'echo $HOME'],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        if has_userns:
            assert result.stdout.strip() == f'/home/{HOST_USER}'

    def test_passwd_home_not_workdir(self, workdir_image, has_userns, run_live_rm):
        """HOME in /etc/passwd should be /home/<user>, not /app."""
        result = run_live_rm(
            [
                '--user-overlay',
                workdir_image,
                'sh',
                '-c',
                f'grep "^{HOST_USER}:" /etc/passwd | cut -d: -f6',
            ],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        if has_userns:
            assert result.stdout.strip() == f'/home/{HOST_USER}'

    def test_workdir_preserved_as_cwd(self, workdir_image, run_live_rm):
        """The image WORKDIR should still be accessible as a directory."""
        result = run_live_rm(
            ['--user-overlay', workdir_image, 'test', '-d', '/app'],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'


# ---------------------------------------------------------------------------
# TestEnvHomeOverride — Images with ENV HOME=/root baked in
# ---------------------------------------------------------------------------


class TestEnvHomeOverride:
    """Verify HOME is forced to /home/<user> even when the image has
    ``ENV HOME=/root`` baked in.

    Without the explicit ``HOME=...`` export in the entrypoint, podman's
    ``setHomeEnvIfNeeded()`` would not override the image's baked-in HOME.
    """

    @pytest.fixture(scope='class')
    def env_home_image(self, podman_store_flags, podman_env):
        """Build a test image with ENV HOME=/root."""
        store_flags = podman_store_flags or []
        tag = 'podrun-test-envhome:latest'
        containerfile = 'FROM alpine:latest\nENV HOME=/root\n'
        cmd = ['podman'] + store_flags + ['build', '-t', tag, '-f', '-', '.']
        result = subprocess.run(
            cmd,
            input=containerfile,
            capture_output=True,
            text=True,
            timeout=120,
            env=podman_env,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0, f'build failed: {result.stderr}'
        return tag

    def test_home_overridden(self, env_home_image, has_userns, run_live_rm):
        """HOME should be /home/<user>, not /root (the image ENV)."""
        result = run_live_rm(
            ['--user-overlay', env_home_image, 'sh', '-c', 'echo $HOME'],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        if has_userns:
            assert result.stdout.strip() == f'/home/{HOST_USER}'

    def test_passwd_home_overridden(self, env_home_image, has_userns, run_live_rm):
        """HOME in /etc/passwd should be /home/<user>, not /root."""
        result = run_live_rm(
            [
                '--user-overlay',
                env_home_image,
                'sh',
                '-c',
                f'grep "^{HOST_USER}:" /etc/passwd | cut -d: -f6',
            ],
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        if has_userns:
            assert result.stdout.strip() == f'/home/{HOST_USER}'

    def test_exec_home_overridden(
        self, env_home_image, podman_run, podman_env, has_userns, podman_store_flags, container_name
    ):
        """HOME in exec session should also be /home/<user>, not /root."""
        name = _start_detached(
            env_home_image,
            podman_env,
            podman_store_flags,
            name=container_name('podrun-test-envhome-exec'),
        )
        try:
            result = podman_run(['exec', name, EXEC_EP, '', '', 'sh', '-c', 'echo $HOME'])
            assert result.returncode == 0, f'stderr: {result.stderr}'
            if has_userns:
                assert result.stdout.strip() == f'/home/{HOST_USER}'
        finally:
            podman_run(['rm', '-f', '-t', '0', name])


# ---------------------------------------------------------------------------
# TestExportPersistence — Export mounts survive in exec sessions
# ---------------------------------------------------------------------------


class TestExportPersistence:
    """Verify exported paths are accessible in exec-entrypoint.sh sessions."""

    def test_export_visible_in_exec(
        self,
        distro_image,
        podman_run,
        podman_env,
        has_userns,
        podman_store_flags,
        tmp_path,
        container_name,
    ):
        """An export created at run time is still a symlink in exec sessions."""
        import hashlib

        host_dir = tmp_path / 'test-export-exec'
        staging_hash = hashlib.sha256('/etc/profile.d'.encode()).hexdigest()[:12]

        name = _start_detached(
            distro_image,
            podman_env,
            podman_store_flags,
            name=container_name('podrun-test-export-exec'),
            extra_args=['--export', f'/etc/profile.d:{host_dir}'],
        )
        try:
            result = podman_run(['exec', name, EXEC_EP, '', '', 'readlink', '/etc/profile.d'])
            assert result.returncode == 0, f'stderr: {result.stderr}'
            assert f'/.podrun/exports/{staging_hash}' == result.stdout.strip()
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_export_content_readable_in_exec(
        self,
        distro_image,
        podman_run,
        podman_env,
        has_userns,
        podman_store_flags,
        tmp_path,
        container_name,
    ):
        """Exported content is readable via the symlink in exec sessions."""
        host_dir = tmp_path / 'test-export-content-exec'

        name = _start_detached(
            distro_image,
            podman_env,
            podman_store_flags,
            name=container_name('podrun-test-export-content'),
            extra_args=['--export', f'/etc/profile.d:{host_dir}'],
        )
        try:
            result = podman_run(['exec', name, EXEC_EP, '', '', 'ls', '/etc/profile.d'])
            assert result.returncode == 0, f'stderr: {result.stderr}'
            # Alpine has at least color_prompt.sh in profile.d
            assert result.stdout.strip() != '', 'export dir should have content'
        finally:
            podman_run(['rm', '-f', '-t', '0', name])


# ---------------------------------------------------------------------------
# TestStoreActivateFunctional — Store activate/deactivate in real shells
# ---------------------------------------------------------------------------


class TestStoreActivateFunctional:
    """Run the activate script in a real shell and verify behavior.

    These tests use the project-local store bootstrapped by the ``podman_store``
    fixture, so they validate the actual store init output.
    """

    def test_activate_prepends_bin_to_path(self, live_store, podman_env):
        """Activation prepends store bin/ to PATH."""
        store_dir = live_store()
        result = subprocess.run(
            ['bash', '-c', f'source "{store_dir}/activate" && echo "$PATH"'],
            capture_output=True,
            text=True,
            timeout=10,
            env=podman_env,
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert str(store_dir / 'bin') in result.stdout.split(':')[0]

    def test_activate_sets_ps1(self, live_store, podman_env):
        """Activation adds (podrun-store) prefix to PS1."""
        store_dir = live_store()
        result = subprocess.run(
            ['bash', '-c', f'PS1="$ " && source "{store_dir}/activate" && echo "$PS1"'],
            capture_output=True,
            text=True,
            timeout=10,
            env=podman_env,
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert '(podrun-store)' in result.stdout

    def test_deactivate_restores_path(self, live_store, podman_env):
        """Deactivation restores PATH to pre-activation value."""
        store_dir = live_store()
        result = subprocess.run(
            [
                'bash',
                '-c',
                f'OLD_PATH="$PATH" && '
                f'source "{store_dir}/activate" && '
                f'deactivate_podrun_store && '
                f'[ "$PATH" = "$OLD_PATH" ] && echo "PATH_RESTORED"',
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=podman_env,
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert 'PATH_RESTORED' in result.stdout

    def test_deactivate_restores_ps1(self, live_store, podman_env):
        """Deactivation restores PS1 to pre-activation value."""
        store_dir = live_store()
        result = subprocess.run(
            [
                'bash',
                '-c',
                f'PS1="original$ " && '
                f'source "{store_dir}/activate" && '
                f'deactivate_podrun_store && '
                f'printf "%s\\n" "$PS1"',
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=podman_env,
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert result.stdout.strip() == 'original$'

    def test_activate_creates_runroot_dir(self, live_store, podman_env):
        """Activation recreates the /tmp runroot dir (post-reboot scenario)."""
        store_dir = live_store()
        runroot_target = os.readlink(str(store_dir / 'runroot'))
        # Simulate reboot: delete the /tmp runroot dir
        if os.path.exists(runroot_target):
            os.rmdir(runroot_target)
        assert not os.path.exists(runroot_target)
        result = subprocess.run(
            ['bash', '-c', f'source "{store_dir}/activate" && echo "ok"'],
            capture_output=True,
            text=True,
            timeout=10,
            env=podman_env,
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert os.path.exists(runroot_target)

    def test_registries_conf_set_and_restored(self, live_store, podman_env):
        """Activation sets CONTAINERS_REGISTRIES_CONF; deactivation restores it."""
        store_dir = live_store(
            extra_args=['--registry', 'mirror.example.com'], name='test-store-reg'
        )
        reg_path = str(store_dir / 'registries.conf')
        # Capture the pre-activation value so we can verify restoration
        pre_val = podman_env.get('CONTAINERS_REGISTRIES_CONF', '')
        result = subprocess.run(
            [
                'bash',
                '-c',
                f'source "{store_dir}/activate" && '
                f'echo "$CONTAINERS_REGISTRIES_CONF" && '
                f'deactivate_podrun_store && '
                f'echo "${{CONTAINERS_REGISTRIES_CONF:-unset}}"',
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=podman_env,
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        lines = result.stdout.strip().splitlines()
        assert lines[0] == reg_path
        # Deactivation restores to whatever was set before activation
        expected_restored = pre_val if pre_val else 'unset'
        assert lines[1] == expected_restored


# ---------------------------------------------------------------------------
# TestContainerStateDetection — Live container state detection
# ---------------------------------------------------------------------------


class TestContainerStateDetection:
    """Validate detect_container_state() and query_container_info() against
    real containers using project-local storage."""

    def test_detect_running(self, distro_image, podman_run, podman_store_flags, container_name):
        from podrun.podrun import detect_container_state

        name = container_name('podrun-test-detect-running')
        podman_run(['run', '-d', '--name', name, distro_image, 'sleep', '60'])
        try:
            assert detect_container_state(name, global_flags=podman_store_flags) == 'running'
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_detect_stopped(self, distro_image, podman_run, podman_store_flags, container_name):
        from podrun.podrun import detect_container_state

        name = container_name('podrun-test-detect-stopped')
        podman_run(['run', '--name', name, distro_image, 'true'])
        try:
            assert detect_container_state(name, global_flags=podman_store_flags) == 'stopped'
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_detect_nonexistent(self, podman_store_flags):
        from podrun.podrun import detect_container_state

        assert (
            detect_container_state('nonexistent-podrun-test', global_flags=podman_store_flags)
            is None
        )

    def test_query_overlay_container(
        self, distro_image, podman_run, podman_env, podman_store_flags, container_name
    ):
        from podrun.podrun import query_container_info

        name = container_name('podrun-test-query-overlay')
        _start_detached(distro_image, podman_env, podman_store_flags, name=name)
        try:
            workdir, overlays = query_container_info(name, global_flags=podman_store_flags)
            assert 'user' in overlays
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_query_bare_container(
        self, distro_image, podman_run, podman_store_flags, container_name
    ):
        from podrun.podrun import query_container_info

        name = container_name('podrun-test-query-bare')
        podman_run(['run', '--name', name, distro_image, 'true'])
        try:
            workdir, overlays = query_container_info(name, global_flags=podman_store_flags)
            assert workdir == ''
            assert overlays == ''
        finally:
            podman_run(['rm', '-f', '-t', '0', name])


# ---------------------------------------------------------------------------
# TestContainerLifecycleEndToEnd — Full replace/attach/start via _run_podrun
# ---------------------------------------------------------------------------


class TestContainerLifecycleEndToEnd:
    """Test the full replace/attach/start workflow via _run_podrun with --print-cmd."""

    def test_replace_stopped_prints_run(
        self, distro_image, podman_run, podman_env, podman_store_flags, container_name
    ):
        name = container_name('podrun-test-replace-stopped')
        _run_podrun(
            [f'--name={name}', distro_image, 'true'],
            podman_env,
            podman_store_flags=podman_store_flags,
        )
        try:
            result = _run_podrun(
                ['--auto-replace', f'--name={name}', '--print-cmd', distro_image],
                podman_env,
                podman_store_flags=podman_store_flags,
            )
            assert result.returncode == 0, f'stderr: {result.stderr}'
            assert 'run' in result.stdout
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_replace_running_prints_run(
        self, distro_image, podman_run, podman_env, podman_store_flags, container_name
    ):
        name = container_name('podrun-test-replace-running')
        podman_run(['run', '-d', '--name', name, distro_image, 'sleep', '60'])
        try:
            result = _run_podrun(
                ['--auto-replace', f'--name={name}', '--print-cmd', distro_image],
                podman_env,
                podman_store_flags=podman_store_flags,
            )
            assert result.returncode == 0, f'stderr: {result.stderr}'
            assert 'run' in result.stdout
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_attach_running_prints_exec(
        self, distro_image, podman_run, podman_env, podman_store_flags, container_name
    ):
        name = container_name('podrun-test-attach-running')
        _start_detached(distro_image, podman_env, podman_store_flags, name=name)
        try:
            result = _run_podrun(
                ['--auto-attach', '--user-overlay', f'--name={name}', '--print-cmd', distro_image],
                podman_env,
                podman_store_flags=podman_store_flags,
            )
            assert result.returncode == 0, f'stderr: {result.stderr}'
            assert 'exec' in result.stdout
            assert name in result.stdout
            assert 'exec-entrypoint.sh' in result.stdout
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_stopped_auto_attach_warns(
        self, distro_image, podman_run, podman_env, podman_store_flags, container_name
    ):
        """Stopped + --auto-attach → warns can't attach, exits 0."""
        name = container_name('podrun-test-stopped-warn')
        _start_detached(distro_image, podman_env, podman_store_flags, name=name)
        podman_run(['stop', '-t', '0', name])
        try:
            result = _run_podrun(
                ['--auto-attach', '--user-overlay', f'--name={name}', '--print-cmd', distro_image],
                podman_env,
                podman_store_flags=podman_store_flags,
            )
            assert result.returncode == 0, f'stderr: {result.stderr}'
            assert 'cannot auto-attach' in result.stderr.lower()
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_attach_global_flags_in_exec(
        self, distro_image, podman_run, podman_env, podman_store_flags, container_name
    ):
        name = container_name('podrun-test-attach-gflags')
        _start_detached(distro_image, podman_env, podman_store_flags, name=name)
        try:
            result = _run_podrun(
                ['--auto-attach', '--user-overlay', f'--name={name}', '--print-cmd', distro_image],
                podman_env,
                podman_store_flags=podman_store_flags,
            )
            assert result.returncode == 0, f'stderr: {result.stderr}'
            parts = result.stdout.strip().split()
            exec_idx = parts.index('exec')
            # Store flags (e.g. --root) must appear before 'exec'
            assert exec_idx > 1, 'store flags should appear before exec'
        finally:
            podman_run(['rm', '-f', '-t', '0', name])

    def test_stopped_both_flags_replace_wins(
        self, distro_image, podman_run, podman_env, podman_store_flags, container_name
    ):
        """Stopped + both --auto-attach and --auto-replace → can't attach, falls through to replace (prints run)."""
        name = container_name('podrun-test-replace-wins')
        _start_detached(distro_image, podman_env, podman_store_flags, name=name)
        podman_run(['stop', '-t', '0', name])
        try:
            result = _run_podrun(
                [
                    '--auto-attach',
                    '--auto-replace',
                    '--user-overlay',
                    f'--name={name}',
                    '--print-cmd',
                    distro_image,
                ],
                podman_env,
                podman_store_flags=podman_store_flags,
            )
            assert result.returncode == 0, f'stderr: {result.stderr}'
            assert 'cannot auto-attach' in result.stderr.lower()
            assert 'run' in result.stdout
        finally:
            podman_run(['rm', '-f', '-t', '0', name])


# ---------------------------------------------------------------------------
# TestSubcommandPassthroughLive — Live subcommand passthrough
# ---------------------------------------------------------------------------


class TestSubcommandPassthroughLive:
    """Test podman subcommand passthrough with real podman."""

    def test_podrun_ps(self, podman_env, podman_store_flags):
        store_flags = podman_store_flags or []
        result = subprocess.run(
            [sys.executable, '-m', 'podrun'] + store_flags + ['ps'],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(PROJECT_ROOT),
            env=podman_env,
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'

    def test_podrun_images(self, podman_env, podman_store_flags):
        store_flags = podman_store_flags or []
        result = subprocess.run(
            [sys.executable, '-m', 'podrun'] + store_flags + ['images'],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(PROJECT_ROOT),
            env=podman_env,
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'

    def test_podrun_completion_bash(self, podman_env, podman_store_flags):
        store_flags = podman_store_flags or []
        result = subprocess.run(
            [sys.executable, '-m', 'podrun'] + store_flags + ['run', '--completion', 'bash'],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(PROJECT_ROOT),
            env=podman_env,
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert 'podrun' in result.stdout

    def test_podrun_inspect_container(
        self, alpine_image, podman_run, podman_env, podman_store_flags, container_name
    ):
        name = container_name('podrun-test-inspect')
        podman_run(['run', '-d', '--name', name, alpine_image, 'sleep', '30'])
        try:
            store_flags = podman_store_flags or []
            result = subprocess.run(
                [sys.executable, '-m', 'podrun'] + store_flags + ['inspect', name],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=str(PROJECT_ROOT),
                env=podman_env,
            )
            assert result.returncode == 0, f'stderr: {result.stderr}'
            assert name in result.stdout
        finally:
            podman_run(['rm', '-f', '-t', '0', name])


# ---------------------------------------------------------------------------
# TestHelpLive — Live help output
# ---------------------------------------------------------------------------


class TestHelpLive:
    """Test help output with real podman."""

    def test_top_level_help(self, podman_env):
        result = subprocess.run(
            [sys.executable, '-m', 'podrun', '--help'],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(PROJECT_ROOT),
            env=podman_env,
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert 'podrun' in result.stdout
        assert 'Podrun:' in result.stdout
        assert 'Available Commands:' in result.stdout
        assert 'store' in result.stdout
        assert 'podrun run --help' in result.stdout

    def test_top_level_help_short_flag(self, podman_env):
        result = subprocess.run(
            [sys.executable, '-m', 'podrun', '-h'],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(PROJECT_ROOT),
            env=podman_env,
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert 'Available Commands:' in result.stdout

    def test_run_help_shows_overlay_options(self, podman_env):
        result = subprocess.run(
            [sys.executable, '-m', 'podrun', 'run', '--help'],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(PROJECT_ROOT),
            env=podman_env,
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert 'Podrun:' in result.stdout
        assert '--user-overlay' in result.stdout


# ---------------------------------------------------------------------------
# TestCheckFlagsLive — Live --check-flags validation
# ---------------------------------------------------------------------------


class TestCheckFlagsLive:
    """Validate --check-flags against the installed podman."""

    def test_check_flags_matches_installed_podman(self, podman_env):
        result = subprocess.run(
            [sys.executable, '-m', 'podrun', 'run', '--check-flags'],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(PROJECT_ROOT),
            env=podman_env,
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert 'match' in result.stdout.lower()


# ---------------------------------------------------------------------------
# TestConfigScriptLive — Live --config-script expansion
# ---------------------------------------------------------------------------


class TestConfigScriptLive:
    """Validate --config-script expansion with real script execution."""

    def test_config_script_expansion(self, distro_image, podman_env, podman_store_flags, tmp_path):
        script = tmp_path / 'config.sh'
        script.write_text('#!/bin/sh\necho "--init"')
        script.chmod(0o755)
        result = _run_podrun(
            [f'--config-script={script}', '--print-cmd', distro_image],
            podman_env,
            podman_store_flags=podman_store_flags,
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert '--init' in result.stdout

    def test_config_script_with_export(
        self, distro_image, podman_env, podman_store_flags, tmp_path
    ):
        """configScript outputs --export; verify it becomes a volume mount, not a podman flag."""
        script = tmp_path / 'config.sh'
        script.write_text('#!/bin/sh\necho "--export /etc/profile.d:./host-dir"')
        script.chmod(0o755)
        result = _run_podrun(
            [f'--config-script={script}', '--user-overlay', '--print-cmd', distro_image],
            podman_env,
            podman_store_flags=podman_store_flags,
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        # --export should NOT appear as a raw podman flag
        assert '--export' not in result.stdout
        # The export should produce a volume mount for the export path
        assert '/etc/profile.d' in result.stdout

    def test_config_script_export_populates_host(
        self, distro_image, podman_env, podman_store_flags, tmp_path
    ):
        """configScript --export of a file actually populates host dir in a live container."""
        host_dir = tmp_path / 'cs-export-file'
        script = tmp_path / 'config.sh'
        script.write_text(f'#!/bin/sh\necho "--export /etc/profile:{host_dir}"')
        script.chmod(0o755)
        result = _run_podrun(
            [
                f'--config-script={script}',
                '--user-overlay',
                '--rm',
                distro_image,
                'cat',
                '/etc/profile',
            ],
            podman_env,
            podman_store_flags=podman_store_flags,
            timeout=60,
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert host_dir.exists(), f'{host_dir} was not created'
        assert (host_dir / 'profile').exists(), (
            f'profile not in {host_dir}: {list(host_dir.iterdir())}'
        )


# ---------------------------------------------------------------------------
# TestOverlayExpansionLive — Live overlay expansion
# ---------------------------------------------------------------------------


class TestOverlayExpansionLive:
    """Validate --workspace and --adhoc expand to correct overlay lists."""

    def test_workspace_overlay_env(self, distro_image, podman_env, podman_store_flags):
        result = _run_podrun(
            ['--workspace', '--print-cmd', distro_image],
            podman_env,
            podman_store_flags=podman_store_flags,
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert 'PODRUN_OVERLAYS=user,host,interactive,workspace' in result.stdout

    def test_adhoc_overlay_env_with_rm(self, distro_image, podman_env, podman_store_flags):
        result = _run_podrun(
            ['--adhoc', '--print-cmd', distro_image],
            podman_env,
            podman_store_flags=podman_store_flags,
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert 'PODRUN_OVERLAYS=user,host,interactive,workspace,adhoc' in result.stdout
        assert '--rm' in result.stdout

    def test_adhoc_no_double_rm(self, distro_image, podman_env, podman_store_flags):
        result = _run_podrun(
            ['--adhoc', '--rm', '--print-cmd', distro_image],
            podman_env,
            podman_store_flags=podman_store_flags,
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert result.stdout.count('--rm') == 1
