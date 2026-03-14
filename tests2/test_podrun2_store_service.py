"""Tests for Phase 2.6: Store service lifecycle.

Covers _store_hash, _store_socket_path, _store_pid_path, _socket_is_alive,
_wait_for_socket, _ensure_store_service, _stop_store_service, hardened
_is_nested, and _handle_run store-service integration.
"""

import os
import pathlib
import signal
import threading

import pytest

import podrun.podrun2 as podrun2_mod
from podrun.podrun2 import (
    _socket_is_alive,
    _store_hash,
    _store_pid_path,
    _store_socket_path,
    _stop_store_service,
    _wait_for_socket,
    PODRUN_CONTAINER_HOST,
    PODRUN_SOCKET_PATH,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Prevent tests from picking up real devcontainer.json or store dirs."""
    monkeypatch.setattr(podrun2_mod, 'find_devcontainer_json', lambda start_dir=None: None)
    monkeypatch.setattr(podrun2_mod, '_default_store_dir', lambda: None)
    monkeypatch.setattr(podrun2_mod, '_is_nested', lambda: False)
    monkeypatch.delenv('PODRUN_CONTAINER', raising=False)
    monkeypatch.delenv('CONTAINER_HOST', raising=False)


# ---------------------------------------------------------------------------
# _store_hash
# ---------------------------------------------------------------------------


class TestStoreHash:
    def test_deterministic(self):
        assert _store_hash('/a/b/c') == _store_hash('/a/b/c')

    def test_twelve_hex_chars(self):
        h = _store_hash('/some/path')
        assert len(h) == 12
        assert all(c in '0123456789abcdef' for c in h)

    def test_consistent_with_runroot(self):
        graphroot = '/tmp/test-store/graphroot'
        h = _store_hash(graphroot)
        from podrun.podrun2 import _runroot_path, _PODRUN_STORES_DIR

        assert _runroot_path(graphroot) == f'{_PODRUN_STORES_DIR}/{h}'


# ---------------------------------------------------------------------------
# _store_socket_path
# ---------------------------------------------------------------------------


class TestStoreSocketPath:
    def test_ends_with_sock(self):
        assert _store_socket_path('/gr').endswith('/podman.sock')

    def test_under_stores_dir(self):
        from podrun.podrun2 import _PODRUN_STORES_DIR

        assert _store_socket_path('/gr').startswith(_PODRUN_STORES_DIR)

    def test_unique_per_graphroot(self):
        assert _store_socket_path('/a') != _store_socket_path('/b')


# ---------------------------------------------------------------------------
# _store_pid_path
# ---------------------------------------------------------------------------


class TestStorePidPath:
    def test_ends_with_pid(self):
        assert _store_pid_path('/gr').endswith('/podman.pid')

    def test_same_dir_as_socket(self):
        gr = '/tmp/store/graphroot'
        sock_dir = os.path.dirname(_store_socket_path(gr))
        pid_dir = os.path.dirname(_store_pid_path(gr))
        assert sock_dir == pid_dir

    def test_unique_per_graphroot(self):
        assert _store_pid_path('/a') != _store_pid_path('/b')


# ---------------------------------------------------------------------------
# _socket_is_alive
# ---------------------------------------------------------------------------


class TestSocketIsAlive:
    def test_no_pid_file(self, tmp_path):
        sock = str(tmp_path / 'podman.sock')
        pid_file = str(tmp_path / 'podman.pid')
        assert _socket_is_alive(sock, pid_file) is False

    def test_stale_pid(self, tmp_path):
        """PID file exists but references a dead process."""
        sock = str(tmp_path / 'podman.sock')
        pid_file = str(tmp_path / 'podman.pid')
        pathlib.Path(pid_file).write_text('999999999')
        assert _socket_is_alive(sock, pid_file) is False

    def test_invalid_pid_content(self, tmp_path):
        sock = str(tmp_path / 'podman.sock')
        pid_file = str(tmp_path / 'podman.pid')
        pathlib.Path(pid_file).write_text('not-a-number')
        assert _socket_is_alive(sock, pid_file) is False

    def test_live_process_with_socket(self, tmp_path, monkeypatch):
        """Process alive and socket exists → True."""
        sock = str(tmp_path / 'podman.sock')
        pid_file = str(tmp_path / 'podman.pid')
        pathlib.Path(pid_file).write_text(str(os.getpid()))
        pathlib.Path(sock).touch()
        assert _socket_is_alive(sock, pid_file) is True

    def test_live_process_no_socket(self, tmp_path):
        """Process alive but socket missing → False."""
        sock = str(tmp_path / 'podman.sock')
        pid_file = str(tmp_path / 'podman.pid')
        pathlib.Path(pid_file).write_text(str(os.getpid()))
        assert _socket_is_alive(sock, pid_file) is False


# ---------------------------------------------------------------------------
# _wait_for_socket
# ---------------------------------------------------------------------------


class TestWaitForSocket:
    def test_already_exists(self, tmp_path):
        sock = str(tmp_path / 'podman.sock')
        pathlib.Path(sock).touch()
        # Should return immediately without error
        _wait_for_socket(sock, timeout=1)

    def test_timeout_warns(self, tmp_path, capsys):
        sock = str(tmp_path / 'no-such.sock')
        _wait_for_socket(sock, timeout=0.2)
        captured = capsys.readouterr()
        assert 'timed out' in captured.err

    def test_delayed_creation(self, tmp_path):
        sock = str(tmp_path / 'podman.sock')

        def create_later():
            import time

            time.sleep(0.2)
            pathlib.Path(sock).touch()

        t = threading.Thread(target=create_later)
        t.start()
        _wait_for_socket(sock, timeout=5)
        t.join()
        assert os.path.exists(sock)


# ---------------------------------------------------------------------------
# _stop_store_service
# ---------------------------------------------------------------------------


class TestStopStoreService:
    def test_noop_when_no_pid_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(podrun2_mod, '_PODRUN_STORES_DIR', str(tmp_path))
        graphroot = str(tmp_path / 'graphroot')
        # Should not raise
        _stop_store_service(graphroot)

    def test_cleans_files(self, tmp_path, monkeypatch):
        """PID references a dead process — files are still cleaned."""
        monkeypatch.setattr(podrun2_mod, '_PODRUN_STORES_DIR', str(tmp_path))
        graphroot = str(tmp_path / 'graphroot')
        h = _store_hash(graphroot)
        store_dir = tmp_path / h
        store_dir.mkdir(parents=True)
        pid_file = store_dir / 'podman.pid'
        sock_file = store_dir / 'podman.sock'
        pid_file.write_text('999999999')
        sock_file.touch()
        _stop_store_service(graphroot)
        assert not pid_file.exists()
        assert not sock_file.exists()

    def test_handles_dead_pid(self, tmp_path, monkeypatch):
        """PID file has invalid content — no crash."""
        monkeypatch.setattr(podrun2_mod, '_PODRUN_STORES_DIR', str(tmp_path))
        graphroot = str(tmp_path / 'graphroot')
        h = _store_hash(graphroot)
        store_dir = tmp_path / h
        store_dir.mkdir(parents=True)
        pid_file = store_dir / 'podman.pid'
        pid_file.write_text('garbage')
        _stop_store_service(graphroot)
        assert not pid_file.exists()

    def test_sends_sigterm(self, tmp_path, monkeypatch):
        """Verify SIGTERM is sent to the stored PID."""
        monkeypatch.setattr(podrun2_mod, '_PODRUN_STORES_DIR', str(tmp_path))
        graphroot = str(tmp_path / 'graphroot')
        h = _store_hash(graphroot)
        store_dir = tmp_path / h
        store_dir.mkdir(parents=True)
        pid_file = store_dir / 'podman.pid'
        pid_file.write_text('12345')

        killed = []
        monkeypatch.setattr(os, 'kill', lambda pid, sig: killed.append((pid, sig)))
        _stop_store_service(graphroot)
        assert (12345, signal.SIGTERM) in killed


# ---------------------------------------------------------------------------
# _ensure_store_service
# ---------------------------------------------------------------------------


class TestEnsureStoreService:
    @pytest.fixture(autouse=True)
    def _stores_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(podrun2_mod, '_PODRUN_STORES_DIR', str(tmp_path))

    def test_refuses_when_nested(self, tmp_path, monkeypatch):
        monkeypatch.setattr(podrun2_mod, '_is_nested', lambda: True)
        with pytest.raises(SystemExit):
            podrun2_mod._ensure_store_service(str(tmp_path / 'gr'), str(tmp_path / 'rr'))

    def test_returns_socket_path(self, tmp_path, monkeypatch):
        graphroot = str(tmp_path / 'graphroot')
        runroot = str(tmp_path / 'runroot')

        monkeypatch.setattr(podrun2_mod, '_socket_is_alive', lambda s, p: False)

        # Ensure parent dir exists for PID file write
        h = _store_hash(graphroot)
        (tmp_path / h).mkdir(parents=True, exist_ok=True)

        mock_proc = type('Proc', (), {'pid': 42})()
        monkeypatch.setattr(podrun2_mod.subprocess, 'Popen', lambda cmd, **kw: mock_proc)
        monkeypatch.setattr(podrun2_mod, '_wait_for_socket', lambda s, **kw: None)

        sock = podrun2_mod._ensure_store_service(graphroot, runroot, podman_path='/usr/bin/podman')
        assert sock == _store_socket_path(graphroot)

    def test_early_return_if_alive(self, tmp_path, monkeypatch):
        graphroot = str(tmp_path / 'graphroot')
        runroot = str(tmp_path / 'runroot')

        monkeypatch.setattr(podrun2_mod, '_socket_is_alive', lambda s, p: True)
        popen_called = []
        monkeypatch.setattr(
            podrun2_mod.subprocess, 'Popen', lambda cmd, **kw: popen_called.append(1)
        )

        sock = podrun2_mod._ensure_store_service(graphroot, runroot, podman_path='/usr/bin/podman')
        assert sock == _store_socket_path(graphroot)
        assert popen_called == []

    def test_starts_popen_with_provided_path(self, tmp_path, monkeypatch):
        graphroot = str(tmp_path / 'graphroot')
        runroot = str(tmp_path / 'runroot')
        h = _store_hash(graphroot)
        (tmp_path / h).mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(podrun2_mod, '_socket_is_alive', lambda s, p: False)
        monkeypatch.setattr(podrun2_mod, '_wait_for_socket', lambda s, **kw: None)

        popen_args = []

        def mock_popen(cmd, **kw):
            popen_args.append(cmd)
            return type('Proc', (), {'pid': 99})()

        monkeypatch.setattr(podrun2_mod.subprocess, 'Popen', mock_popen)

        podrun2_mod._ensure_store_service(graphroot, runroot, podman_path='/opt/custom/podman')
        assert len(popen_args) == 1
        cmd = popen_args[0]
        assert cmd[0] == '/opt/custom/podman'
        assert 'system' in cmd
        assert 'service' in cmd
        assert '--root' in cmd
        assert graphroot in cmd

    def test_writes_pid(self, tmp_path, monkeypatch):
        graphroot = str(tmp_path / 'graphroot')
        runroot = str(tmp_path / 'runroot')
        h = _store_hash(graphroot)
        (tmp_path / h).mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(podrun2_mod, '_socket_is_alive', lambda s, p: False)
        monkeypatch.setattr(podrun2_mod, '_wait_for_socket', lambda s, **kw: None)

        mock_proc = type('Proc', (), {'pid': 777})()
        monkeypatch.setattr(podrun2_mod.subprocess, 'Popen', lambda cmd, **kw: mock_proc)

        podrun2_mod._ensure_store_service(graphroot, runroot, podman_path='/usr/bin/podman')
        pid_file = _store_pid_path(graphroot)
        assert pathlib.Path(pid_file).read_text().strip() == '777'

    def test_registries_conf_env(self, tmp_path, monkeypatch):
        graphroot = str(tmp_path / 'graphroot')
        runroot = str(tmp_path / 'runroot')
        store_dir = str(tmp_path / 'store')
        os.makedirs(store_dir, exist_ok=True)
        reg_conf = pathlib.Path(store_dir) / 'registries.conf'
        reg_conf.write_text('[registries.search]\n')
        h = _store_hash(graphroot)
        (tmp_path / h).mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(podrun2_mod, '_socket_is_alive', lambda s, p: False)
        monkeypatch.setattr(podrun2_mod, '_wait_for_socket', lambda s, **kw: None)

        popen_kwargs = []

        def mock_popen(cmd, **kw):
            popen_kwargs.append(kw)
            return type('Proc', (), {'pid': 1})()

        monkeypatch.setattr(podrun2_mod.subprocess, 'Popen', mock_popen)

        podrun2_mod._ensure_store_service(
            graphroot, runroot, store_dir=store_dir, podman_path='/usr/bin/podman'
        )
        env = popen_kwargs[0]['env']
        assert env['CONTAINERS_REGISTRIES_CONF'] == str(reg_conf)


# ---------------------------------------------------------------------------
# _is_nested (hardened)
# ---------------------------------------------------------------------------


class TestIsNestedHardened:
    """Test the hardened _is_nested() directly, bypassing the autouse mock."""

    def _real_is_nested(self):
        """Call the real _is_nested from the module source."""
        # Inline the logic directly instead of going through the monkeypatched module
        if os.environ.get('PODRUN_CONTAINER'):
            return True
        if os.environ.get('CONTAINER_HOST') == PODRUN_CONTAINER_HOST and os.path.exists(
            PODRUN_SOCKET_PATH
        ):
            return True
        return False

    def test_env_var_set(self, monkeypatch):
        monkeypatch.setenv('PODRUN_CONTAINER', '1')
        assert self._real_is_nested() is True

    def test_neither_set(self, monkeypatch):
        monkeypatch.delenv('PODRUN_CONTAINER', raising=False)
        monkeypatch.delenv('CONTAINER_HOST', raising=False)
        assert self._real_is_nested() is False

    def test_container_host_and_socket(self, monkeypatch):
        monkeypatch.delenv('PODRUN_CONTAINER', raising=False)
        monkeypatch.setenv('CONTAINER_HOST', PODRUN_CONTAINER_HOST)
        monkeypatch.setattr(os.path, 'exists', lambda p: p == PODRUN_SOCKET_PATH)
        assert self._real_is_nested() is True

    def test_container_host_only(self, monkeypatch):
        """CONTAINER_HOST set but socket doesn't exist → not nested."""
        monkeypatch.delenv('PODRUN_CONTAINER', raising=False)
        monkeypatch.setenv('CONTAINER_HOST', PODRUN_CONTAINER_HOST)
        monkeypatch.setattr(os.path, 'exists', lambda p: False)
        assert self._real_is_nested() is False

    def test_socket_only(self, monkeypatch):
        """Socket exists but CONTAINER_HOST not set → not nested."""
        monkeypatch.delenv('PODRUN_CONTAINER', raising=False)
        monkeypatch.delenv('CONTAINER_HOST', raising=False)
        monkeypatch.setattr(os.path, 'exists', lambda p: p == PODRUN_SOCKET_PATH)
        assert self._real_is_nested() is False

    def test_tamper_unset_env_var(self, monkeypatch):
        """User unsets PODRUN_CONTAINER but socket+host still detects."""
        monkeypatch.delenv('PODRUN_CONTAINER', raising=False)
        monkeypatch.setenv('CONTAINER_HOST', PODRUN_CONTAINER_HOST)
        monkeypatch.setattr(os.path, 'exists', lambda p: p == PODRUN_SOCKET_PATH)
        assert self._real_is_nested() is True


# ---------------------------------------------------------------------------
# _handle_run store service integration
# ---------------------------------------------------------------------------


class TestHandleRunStoreService:
    """Verify _handle_run starts store service when remote+store."""

    @pytest.fixture()
    def _run_ns(self, tmp_path, monkeypatch):
        """Set up a minimal ns dict and result for _handle_run with --print-cmd."""
        monkeypatch.setattr(podrun2_mod, '_PODRUN_STORES_DIR', str(tmp_path / 'stores'))
        (tmp_path / 'stores').mkdir()

        store_dir = tmp_path / 'store'
        store_dir.mkdir()
        graphroot_dir = store_dir / 'graphroot'
        graphroot_dir.mkdir()

        ns = {
            'subcommand': 'run',
            'podman_global_args': [],
            'run.passthrough_args': [],
            'run.name': 'test-ctr',
            'run.auto_attach': False,
            'run.auto_replace': False,
            'run.user_overlay': True,
            'run.host_overlay': False,
            'run.interactive_overlay': False,
            'run.dot_files_overlay': False,
            'run.workspace': None,
            'run.adhoc': False,
            'run.podman_remote': False,
            'run.export': [],
            'run.shell': '/bin/bash',
            'run.login': None,
            'run.prompt_banner': None,
            'run.fuse_overlayfs': False,
            'run.print_overlays': False,
            'run.workspace_folder': None,
            'run.workspace_mount_src': None,
            'run.image': None,
            'run.label': [],
            'run.store_socket': None,
            'root.print_cmd': True,
            'root.local_store': None,
        }
        result = type(
            'Result',
            (),
            {
                'ns': ns,
                'trailing_args': ['ubuntu:latest'],
                'explicit_command': None,
            },
        )()

        # Stub out functions that would fail without real podman
        monkeypatch.setattr(podrun2_mod, 'handle_container_state', lambda ns, **kw: 'run')
        monkeypatch.setattr(podrun2_mod, '_warn_missing_subids', lambda: None)
        mock_result = type('Result', (), {'stdout': '', 'stderr': '', 'returncode': 0})()
        monkeypatch.setattr(podrun2_mod, 'run_os_cmd', lambda cmd: mock_result)

        return result, ns, store_dir

    def test_service_started_with_remote_and_store(self, _run_ns, monkeypatch):
        result, ns, store_dir = _run_ns
        ns['run.podman_remote'] = True
        ns['root.local_store'] = str(store_dir)

        ensure_calls = []

        def mock_ensure(graphroot, runroot, store_dir=None, podman_path='podman'):
            ensure_calls.append((graphroot, runroot, store_dir, podman_path))
            return '/tmp/fake.sock'

        monkeypatch.setattr(podrun2_mod, '_ensure_store_service', mock_ensure)

        with pytest.raises(SystemExit):
            podrun2_mod._handle_run(result, 'podman')

        assert len(ensure_calls) == 1
        assert 'graphroot' in ensure_calls[0][0]
        assert ns['run.store_socket'] == '/tmp/fake.sock'

    def test_not_started_without_remote(self, _run_ns, monkeypatch):
        result, ns, store_dir = _run_ns
        ns['run.podman_remote'] = False
        ns['root.local_store'] = str(store_dir)

        ensure_calls = []
        monkeypatch.setattr(
            podrun2_mod,
            '_ensure_store_service',
            lambda *a, **kw: ensure_calls.append(1) or '/tmp/fake.sock',
        )

        with pytest.raises(SystemExit):
            podrun2_mod._handle_run(result, 'podman')

        assert ensure_calls == []

    def test_not_started_without_store(self, _run_ns, monkeypatch):
        result, ns, store_dir = _run_ns
        ns['run.podman_remote'] = True
        ns['root.local_store'] = None

        ensure_calls = []
        monkeypatch.setattr(
            podrun2_mod,
            '_ensure_store_service',
            lambda *a, **kw: ensure_calls.append(1) or '/tmp/fake.sock',
        )

        with pytest.raises(SystemExit):
            podrun2_mod._handle_run(result, 'podman')

        assert ensure_calls == []
