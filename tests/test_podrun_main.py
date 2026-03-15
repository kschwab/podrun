"""Tests for Phase 2.5 — main orchestration + execution."""

import shlex
import subprocess

import pytest

import podrun.podrun as podrun_mod
from podrun.podrun import (
    PODRUN_CONTAINER_HOST,
    PODRUN_SOCKET_PATH,
    UNAME,
    _default_podman_path,
    _filter_global_args,
    _fuse_overlayfs_fixup,
    _is_nested,
    _warn_missing_subids,
    main,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Prevent tests from picking up real devcontainer.json or store dirs."""
    monkeypatch.setattr(podrun_mod, 'find_devcontainer_json', lambda start_dir=None: None)
    monkeypatch.setattr(podrun_mod, '_default_store_dir', lambda: None)
    monkeypatch.setattr(podrun_mod, '_is_nested', lambda: False)
    # Clear nested podrun guard env var (we're running inside a podrun container)
    monkeypatch.delenv('PODRUN_CONTAINER', raising=False)
    monkeypatch.delenv('PODRUN_PODMAN_PATH', raising=False)


# ---------------------------------------------------------------------------
# _default_podman_path
# ---------------------------------------------------------------------------


class TestDefaultPodmanPath:
    def test_returns_podman_normally(self, monkeypatch):
        monkeypatch.delenv('CONTAINER_HOST', raising=False)
        path = _default_podman_path()
        assert path is not None
        assert 'podman' in path

    def test_prefers_remote_inside_container(self, monkeypatch):
        monkeypatch.setenv('CONTAINER_HOST', 'unix:///run/podman/podman.sock')
        monkeypatch.setattr(podrun_mod, '_is_nested', lambda: True)
        original_which = podrun_mod.shutil.which

        def fake_which(name):
            if name == 'podman-remote':
                return '/usr/bin/podman-remote'
            return original_which(name)

        monkeypatch.setattr(podrun_mod.shutil, 'which', fake_which)
        assert _default_podman_path() == '/usr/bin/podman-remote'

    def test_falls_back_when_not_nested(self, monkeypatch):
        monkeypatch.setenv('CONTAINER_HOST', 'unix:///run/podman/podman.sock')
        # autouse fixture already sets _is_nested → False
        path = _default_podman_path()
        assert path is None or 'podman-remote' not in path

    def test_falls_back_without_container_host(self, monkeypatch):
        monkeypatch.delenv('CONTAINER_HOST', raising=False)
        monkeypatch.setattr(podrun_mod, '_is_nested', lambda: True)
        path = _default_podman_path()
        # Without CONTAINER_HOST, should use regular podman
        assert path is None or 'podman-remote' not in path

    def test_podman_path_env_returns_resolved(self, monkeypatch):
        monkeypatch.setenv('PODRUN_PODMAN_PATH', 'podman')
        path = _default_podman_path()
        # shutil.which('podman') should resolve to a real path
        assert path is not None
        assert 'podman' in path

    def test_podman_path_env_invalid_exits(self, monkeypatch):
        monkeypatch.setenv('PODRUN_PODMAN_PATH', 'no-such-binary-xyz')
        with pytest.raises(SystemExit) as exc_info:
            _default_podman_path()
        assert exc_info.value.code == 1

    def test_podman_path_env_invalid_message(self, monkeypatch, capsys):
        monkeypatch.setenv('PODRUN_PODMAN_PATH', 'no-such-binary-xyz')
        with pytest.raises(SystemExit):
            _default_podman_path()
        err = capsys.readouterr().err
        assert 'PODRUN_PODMAN_PATH' in err
        assert 'no-such-binary-xyz' in err

    def test_podman_path_env_overrides_nested_remote(self, monkeypatch):
        """PODRUN_PODMAN_PATH takes priority over nested podman-remote preference."""
        monkeypatch.setenv('CONTAINER_HOST', 'unix:///run/podman/podman.sock')
        monkeypatch.setattr(podrun_mod, '_is_nested', lambda: True)
        # Point to regular podman — should be used even when nested
        monkeypatch.setenv('PODRUN_PODMAN_PATH', 'podman')
        path = _default_podman_path()
        assert path is not None
        assert 'podman-remote' not in path

    def test_podman_path_env_absolute(self, monkeypatch, tmp_path):
        """PODRUN_PODMAN_PATH works with absolute paths."""
        fake_bin = tmp_path / 'my-podman'
        fake_bin.write_text('#!/bin/sh\n')
        fake_bin.chmod(0o755)
        monkeypatch.setenv('PODRUN_PODMAN_PATH', str(fake_bin))
        path = _default_podman_path()
        assert path == str(fake_bin)


# ---------------------------------------------------------------------------
# _is_nested
# ---------------------------------------------------------------------------


class TestIsNested:
    """Test _is_nested() directly — calls the real function, not the autouse mock."""

    def test_env_var_primary(self, monkeypatch):
        monkeypatch.setenv('PODRUN_CONTAINER', '1')
        assert _is_nested() is True

    def test_fallback_container_host_and_socket(self, monkeypatch):
        monkeypatch.delenv('PODRUN_CONTAINER', raising=False)
        monkeypatch.setenv('CONTAINER_HOST', PODRUN_CONTAINER_HOST)
        real_exists = podrun_mod.os.path.exists
        monkeypatch.setattr(
            podrun_mod.os.path,
            'exists',
            lambda p: True if p == PODRUN_SOCKET_PATH else real_exists(p),
        )
        assert _is_nested() is True

    def test_false_without_env_or_socket(self, monkeypatch):
        monkeypatch.delenv('PODRUN_CONTAINER', raising=False)
        monkeypatch.delenv('CONTAINER_HOST', raising=False)
        assert _is_nested() is False


# ---------------------------------------------------------------------------
# _warn_missing_subids
# ---------------------------------------------------------------------------


class TestWarnMissingSubids:
    def test_no_warning_when_present(self, tmp_path, monkeypatch, capsys):
        subuid = tmp_path / 'subuid'
        subgid = tmp_path / 'subgid'
        subuid.write_text(f'{UNAME}:100000:65536\n')
        subgid.write_text(f'{UNAME}:100000:65536\n')
        # Patch the file paths
        original_open = open

        def patched_open(path, *a, **kw):
            if path == '/etc/subuid':
                return original_open(str(subuid), *a, **kw)
            if path == '/etc/subgid':
                return original_open(str(subgid), *a, **kw)
            return original_open(path, *a, **kw)

        monkeypatch.setattr('builtins.open', patched_open)
        _warn_missing_subids()
        assert 'Note:' not in capsys.readouterr().err

    def test_warning_when_missing(self, tmp_path, monkeypatch, capsys):
        subuid = tmp_path / 'subuid'
        subgid = tmp_path / 'subgid'
        subuid.write_text('otheruser:100000:65536\n')
        subgid.write_text('otheruser:100000:65536\n')
        original_open = open

        def patched_open(path, *a, **kw):
            if path == '/etc/subuid':
                return original_open(str(subuid), *a, **kw)
            if path == '/etc/subgid':
                return original_open(str(subgid), *a, **kw)
            return original_open(path, *a, **kw)

        monkeypatch.setattr('builtins.open', patched_open)
        _warn_missing_subids()
        err = capsys.readouterr().err
        assert 'Note:' in err
        assert UNAME in err

    def test_warning_when_files_missing(self, monkeypatch, capsys):
        def patched_open(path, *a, **kw):
            if path in ('/etc/subuid', '/etc/subgid'):
                raise FileNotFoundError(path)
            return open(path, *a, **kw)

        monkeypatch.setattr('builtins.open', patched_open)
        _warn_missing_subids()
        err = capsys.readouterr().err
        assert 'Note:' in err

    def test_no_crash_on_exception(self, monkeypatch):
        def patched_open(path, *a, **kw):
            raise PermissionError('denied')

        monkeypatch.setattr('builtins.open', patched_open)
        _warn_missing_subids()  # should not raise


# ---------------------------------------------------------------------------
# _fuse_overlayfs_fixup
# ---------------------------------------------------------------------------


class TestFuseOverlayfsFixup:
    def test_injects_storage_opt(self, monkeypatch):
        monkeypatch.setattr(
            podrun_mod.shutil,
            'which',
            lambda name: '/usr/bin/fuse-overlayfs' if name == 'fuse-overlayfs' else None,
        )
        ns = {'run.passthrough_args': ['-v=/a:/b']}
        _fuse_overlayfs_fixup(ns)
        gf = ns.get('podman_global_args') or []
        assert '--storage-opt' in gf
        assert 'overlay.mount_program=/usr/bin/fuse-overlayfs' in gf

    def test_converts_overlay_to_ro_for_files_equals_form(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            podrun_mod.shutil,
            'which',
            lambda name: '/usr/bin/fuse-overlayfs' if name == 'fuse-overlayfs' else None,
        )
        f = tmp_path / 'file.txt'
        f.write_text('hi')
        ns = {'run.passthrough_args': [f'-v={f}:/container/file.txt:O']}
        _fuse_overlayfs_fixup(ns)
        assert ns['run.passthrough_args'] == [f'-v={f}:/container/file.txt:ro']

    def test_converts_overlay_to_ro_for_files_space_form(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            podrun_mod.shutil,
            'which',
            lambda name: '/usr/bin/fuse-overlayfs' if name == 'fuse-overlayfs' else None,
        )
        f = tmp_path / 'file.txt'
        f.write_text('hi')
        ns = {'run.passthrough_args': ['-v', f'{f}:/container/file.txt:O']}
        _fuse_overlayfs_fixup(ns)
        assert ns['run.passthrough_args'] == ['-v', f'{f}:/container/file.txt:ro']

    def test_preserves_overlay_for_directories(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            podrun_mod.shutil,
            'which',
            lambda name: '/usr/bin/fuse-overlayfs' if name == 'fuse-overlayfs' else None,
        )
        d = tmp_path / 'dir'
        d.mkdir()
        ns = {'run.passthrough_args': [f'-v={d}:/container/dir:O']}
        _fuse_overlayfs_fixup(ns)
        assert ns['run.passthrough_args'] == [f'-v={d}:/container/dir:O']

    def test_preserves_overlay_for_directories_space_form(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            podrun_mod.shutil,
            'which',
            lambda name: '/usr/bin/fuse-overlayfs' if name == 'fuse-overlayfs' else None,
        )
        d = tmp_path / 'dir'
        d.mkdir()
        ns = {'run.passthrough_args': ['-v', f'{d}:/container/dir:O']}
        _fuse_overlayfs_fixup(ns)
        assert ns['run.passthrough_args'] == ['-v', f'{d}:/container/dir:O']

    def test_non_volume_args_unchanged(self, monkeypatch):
        monkeypatch.setattr(
            podrun_mod.shutil,
            'which',
            lambda name: '/usr/bin/fuse-overlayfs' if name == 'fuse-overlayfs' else None,
        )
        ns = {'run.passthrough_args': ['--rm', '-e', 'FOO=bar', '-it']}
        _fuse_overlayfs_fixup(ns)
        assert ns['run.passthrough_args'] == ['--rm', '-e', 'FOO=bar', '-it']

    def test_exits_when_not_found(self, monkeypatch):
        monkeypatch.setattr(podrun_mod.shutil, 'which', lambda name: None)
        ns = {}
        with pytest.raises(SystemExit):
            _fuse_overlayfs_fixup(ns)

    def test_appends_to_existing_global_args(self, monkeypatch):
        monkeypatch.setattr(
            podrun_mod.shutil,
            'which',
            lambda name: '/usr/bin/fuse-overlayfs' if name == 'fuse-overlayfs' else None,
        )
        ns = {'podman_global_args': ['--root', '/x']}
        _fuse_overlayfs_fixup(ns)
        gf = ns['podman_global_args']
        assert '--root' in gf
        assert '--storage-opt' in gf

    def test_empty_passthrough(self, monkeypatch):
        monkeypatch.setattr(
            podrun_mod.shutil,
            'which',
            lambda name: '/usr/bin/fuse-overlayfs' if name == 'fuse-overlayfs' else None,
        )
        ns = {}
        _fuse_overlayfs_fixup(ns)
        assert ns['run.passthrough_args'] == []


# ---------------------------------------------------------------------------
# _handle_run — through main() with --print-cmd
# ---------------------------------------------------------------------------


class TestHandleRunViaPrintCmd:
    """Test _handle_run indirectly via main(['--print-cmd', 'run', ...])."""

    @pytest.fixture(autouse=True)
    def _tmp_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path))
        # Suppress stale file cleanup
        monkeypatch.setattr(
            podrun_mod,
            'run_os_cmd',
            lambda cmd: subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='',
                stderr='',
            ),
        )

    def _run(self, argv, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(['--print-cmd'] + argv)
        assert exc_info.value.code == 0
        return shlex.split(capsys.readouterr().out)

    def test_bare_run(self, capsys):
        cmd = self._run(['run', 'alpine'], capsys)
        assert 'run' in cmd
        assert 'alpine' in cmd

    def test_user_overlay_run(self, capsys):
        cmd = self._run(['run', '--user-overlay', 'alpine'], capsys)
        assert '--userns=keep-id' in cmd
        assert any(a.startswith('--entrypoint=') for a in cmd)
        assert 'alpine' in cmd

    def test_host_overlay_run(self, capsys):
        cmd = self._run(['run', '--host-overlay', 'alpine'], capsys)
        assert '--network=host' in cmd
        assert '--userns=keep-id' in cmd  # implied

    def test_session_run(self, capsys):
        cmd = self._run(['run', '--session', 'alpine'], capsys)
        assert '-it' in cmd
        assert '--network=host' in cmd
        assert '--userns=keep-id' in cmd

    def test_adhoc_run(self, capsys):
        cmd = self._run(['run', '--adhoc', 'alpine'], capsys)
        assert '--rm' in cmd
        assert '-it' in cmd
        assert '--userns=keep-id' in cmd

    def test_named_container(self, capsys):
        cmd = self._run(['run', '--name=myc', '--session', 'alpine'], capsys)
        assert '--name=myc' in cmd

    def test_with_passthrough_flags(self, capsys):
        cmd = self._run(['run', '--session', '-e', 'A=1', '-v', '/x:/y', 'alpine'], capsys)
        assert '-e' in cmd
        assert 'A=1' in cmd
        assert any('/x:/y' in a or a == '/x:/y' for a in cmd)

    def test_with_command(self, capsys):
        cmd = self._run(['run', 'alpine', 'echo', 'hi'], capsys)
        assert 'echo' in cmd
        assert 'hi' in cmd

    def test_with_separator_command(self, capsys):
        cmd = self._run(['run', 'alpine', '--', 'bash', '-c', 'echo hi'], capsys)
        sep_idx = cmd.index('--')
        assert cmd[sep_idx + 1 :] == ['bash', '-c', 'echo hi']


# ---------------------------------------------------------------------------
# _handle_run — error cases
# ---------------------------------------------------------------------------


class TestHandleRunErrors:
    @pytest.fixture(autouse=True)
    def _tmp_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path))

    def test_no_image_exits(self, capsys):
        with pytest.raises(SystemExit):
            main(['--print-cmd', 'run'])
        assert 'No image' in capsys.readouterr().err

    def test_export_without_user_overlay_exits(self, capsys):
        with pytest.raises(SystemExit):
            main(['--print-cmd', 'run', '--export', '/a:/b', 'alpine'])
        assert '--export requires' in capsys.readouterr().err

    def test_print_overlays_exits(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--print-overlays', 'alpine'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'user:' in out


# ---------------------------------------------------------------------------
# _handle_run — container state actions
# ---------------------------------------------------------------------------


class TestHandleRunContainerState:
    @pytest.fixture(autouse=True)
    def _tmp_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path))

    def test_replace_prints_rm_then_run(self, monkeypatch, capsys):
        """When replacing, --print-cmd should show rm then run command."""
        monkeypatch.setattr(podrun_mod, 'detect_container_state', lambda *a, **kw: 'running')
        with pytest.raises(SystemExit) as exc_info:
            main(['--print-cmd', 'run', '--name=myc', '--auto-replace', '--session', 'alpine'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        lines = out.strip().split('\n')
        assert len(lines) == 2
        assert 'rm -f' in lines[0]
        assert 'myc' in lines[0]
        assert 'run' in lines[1]

    def test_attach_prints_exec(self, monkeypatch, capsys):
        """When attaching, --print-cmd should show exec command."""
        monkeypatch.setattr(podrun_mod, 'detect_container_state', lambda *a, **kw: 'running')
        monkeypatch.setattr(
            podrun_mod, 'query_container_info', lambda *a, **kw: ('/work', 'user,host')
        )
        with pytest.raises(SystemExit) as exc_info:
            main(['--print-cmd', 'run', '--name=myc', '--auto-attach', '--session', 'alpine'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        cmd = shlex.split(out)
        assert 'exec' in cmd
        assert 'myc' in cmd

    def test_attach_non_user_overlay_container_errors(self, monkeypatch, capsys):
        """Cannot attach to a container not created with user overlay."""
        monkeypatch.setattr(podrun_mod, 'detect_container_state', lambda *a, **kw: 'running')
        monkeypatch.setattr(podrun_mod, 'query_container_info', lambda *a, **kw: ('/work', 'none'))
        with pytest.raises(SystemExit):
            main(['--print-cmd', 'run', '--name=myc', '--auto-attach', '--session', 'alpine'])
        assert 'not created with podrun user overlay' in capsys.readouterr().err

    def test_action_none_exits_cleanly(self, monkeypatch):
        """When handle_container_state returns None, main exits with 0."""
        monkeypatch.setattr(podrun_mod, 'detect_container_state', lambda *a, **kw: 'running')
        monkeypatch.setattr(podrun_mod, 'handle_container_state', lambda *a, **kw: None)
        with pytest.raises(SystemExit) as exc_info:
            main(['--print-cmd', 'run', '--name=myc', '--session', 'alpine'])
        assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# _handle_run — export conflict filtering
# ---------------------------------------------------------------------------


class TestExportConflictFiltering:
    @pytest.fixture(autouse=True)
    def _tmp_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path))
        monkeypatch.setattr(
            podrun_mod,
            'run_os_cmd',
            lambda cmd: subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='',
                stderr='',
            ),
        )

    def test_conflicting_export_skipped(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    '--print-cmd',
                    'run',
                    '--session',
                    '-v',
                    '/host:/data',
                    '--export',
                    '/data:/host/export',
                    'alpine',
                ]
            )
        assert exc_info.value.code == 0
        err = capsys.readouterr().err
        assert 'skipped' in err

    def test_non_conflicting_export_preserved(self, capsys, tmp_path):
        export_src = tmp_path / 'export_src'
        export_src.mkdir()
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    '--print-cmd',
                    'run',
                    '--session',
                    '--export',
                    f'/unique:{export_src}',
                    'alpine',
                ]
            )
        assert exc_info.value.code == 0
        cmd = shlex.split(capsys.readouterr().out)
        assert any('exports' in a for a in cmd)


# ---------------------------------------------------------------------------
# main — nested podrun guard
# ---------------------------------------------------------------------------


class TestNestedPodrunExecution:
    """Nested podrun (inside a podrun container) should work, not be refused."""

    def test_version_works_when_nested(self, monkeypatch):
        monkeypatch.setattr(podrun_mod, '_is_nested', lambda: True)
        with pytest.raises(SystemExit) as exc_info:
            main(['--version'])
        assert exc_info.value.code == 0

    def test_passthrough_proceeds_when_nested(self, monkeypatch, capsys):
        monkeypatch.setattr(podrun_mod, '_is_nested', lambda: True)
        with pytest.raises(SystemExit) as exc_info:
            main(['--print-cmd', 'ps', '-a'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'ps' in out

    def test_run_proceeds_when_nested(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(podrun_mod, '_is_nested', lambda: True)
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path))
        monkeypatch.setattr(
            podrun_mod,
            'run_os_cmd',
            lambda cmd: subprocess.CompletedProcess(args=cmd, returncode=0, stdout='', stderr=''),
        )
        with pytest.raises(SystemExit) as exc_info:
            main(['--print-cmd', 'run', 'alpine'])
        assert exc_info.value.code == 0
        cmd = capsys.readouterr().out
        assert 'run' in cmd
        assert 'alpine' in cmd

    def test_local_store_destroy_still_errors_when_nested(self, monkeypatch, capsys):
        monkeypatch.setattr(podrun_mod, '_is_nested', lambda: True)
        with pytest.raises(SystemExit) as exc_info:
            main(['--local-store', '/tmp/s', '--local-store-destroy'])
        assert exc_info.value.code == 1
        assert 'not supported' in capsys.readouterr().err

    def test_no_guard_without_env(self, monkeypatch):
        # autouse fixture already sets _is_nested → False
        # Just verify it doesn't exit with the nested error
        try:
            main(['--version'])
        except SystemExit as e:
            assert e.code == 0


# ---------------------------------------------------------------------------
# main — podman path resolution
# ---------------------------------------------------------------------------


class TestMainPodmanPath:
    def test_uses_default_podman_path(self, monkeypatch, capsys):
        """main() should use _default_podman_path() for podman resolution."""
        called = {}

        def fake_default():
            called['yes'] = True
            return podrun_mod.shutil.which('podman')

        monkeypatch.setattr(podrun_mod, '_default_podman_path', fake_default)
        with pytest.raises(SystemExit):
            main(['--version'])
        assert called.get('yes')


# ---------------------------------------------------------------------------
# _filter_global_args
# ---------------------------------------------------------------------------


class TestFilterGlobalArgs:
    """Test cache-aware global flag filtering."""

    @pytest.fixture()
    def flags(self):
        from podrun.podrun import PodmanFlags

        return PodmanFlags(
            global_value_flags=frozenset(['--log-level', '--network']),
            global_boolean_flags=frozenset(['--noout']),
            subcommands=frozenset(),
            run_value_flags=frozenset(),
            run_boolean_flags=frozenset(),
        )

    def test_drops_unknown_value_flags(self, flags):
        args = ['--root', '/x', '--log-level', 'debug']
        result = _filter_global_args(args, flags)
        assert result == ['--log-level', 'debug']

    def test_keeps_known_flags(self, flags):
        args = ['--log-level', 'debug', '--noout']
        result = _filter_global_args(args, flags)
        assert result == ['--log-level', 'debug', '--noout']

    def test_drops_multiple_unknown(self, flags):
        args = ['--root', '/x', '--runroot', '/y', '--storage-driver', 'overlay']
        result = _filter_global_args(args, flags)
        assert result == []

    def test_empty_input(self, flags):
        assert _filter_global_args([], flags) == []

    def test_mixed_known_unknown(self, flags):
        args = ['--log-level', 'debug', '--root', '/x', '--noout', '--storage-opt', 'foo']
        result = _filter_global_args(args, flags)
        assert result == ['--log-level', 'debug', '--noout']

    def test_non_flag_values_preserved(self, flags):
        # Non-flag args (values without leading -) pass through
        args = ['--log-level', 'debug']
        result = _filter_global_args(args, flags)
        assert result == ['--log-level', 'debug']


# ---------------------------------------------------------------------------
# Nested guard behavior in _handle_run
# ---------------------------------------------------------------------------


class TestNestedHandleRunGuards:
    """Verify _warn_missing_subids and _fuse_overlayfs_fixup are skipped when nested."""

    @pytest.fixture(autouse=True)
    def _tmp_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path))
        monkeypatch.setattr(
            podrun_mod,
            'run_os_cmd',
            lambda cmd: subprocess.CompletedProcess(args=cmd, returncode=0, stdout='', stderr=''),
        )

    def test_warn_subids_skipped_when_nested(self, monkeypatch, capsys):
        monkeypatch.setattr(podrun_mod, '_is_nested', lambda: True)
        with pytest.raises(SystemExit) as exc_info:
            main(['--print-cmd', 'run', '--session', 'alpine'])
        assert exc_info.value.code == 0
        err = capsys.readouterr().err
        # Should not contain subuid/subgid warnings when nested
        assert 'subuid' not in err.lower()

    def test_fuse_fixup_skipped_when_nested(self, monkeypatch, capsys):
        monkeypatch.setattr(podrun_mod, '_is_nested', lambda: True)
        with pytest.raises(SystemExit) as exc_info:
            main(['--print-cmd', 'run', '--fuse-overlayfs', 'alpine'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        # --storage-opt should NOT be injected when nested
        assert '--storage-opt' not in out
