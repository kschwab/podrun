"""Tests for Phase 2.5 — main orchestration + execution."""

import os
import shlex
import shutil
import subprocess

import pytest

import podrun.podrun as podrun_mod
from podrun.podrun import (
    ENV_PODRUN_CONTAINER,
    ENV_PODRUN_PODMAN_PATH,
    ENV_PODRUN_PODMAN_REMOTE,
    PodmanFlags,
    UNAME,
    PodrunContext,
    _NFS_REMEDIATE_DEFAULT_BASE,
    _clean_stale_cache,
    _default_podman_path,
    _discover_podrunrc,
    _filter_global_args,
    _flags_cache_path,
    _is_network_fs,
    _is_remote,
    _is_vacant_store,
    _nfs_remediate,
    _resolve_overlay_mounts,
    _run_initialize_command,
    _warn_missing_subids,
    _write_flags_cache,
    _cleanup,
    load_podman_flags,
    main,
    parse_args,
)


pytestmark = pytest.mark.usefixtures('podman_binary')


# ---------------------------------------------------------------------------
# _default_podman_path
# ---------------------------------------------------------------------------


class TestDefaultPodmanPath:
    def test_returns_podman_normally(self, monkeypatch):
        path = _default_podman_path()
        assert path is not None
        assert 'podman' in path

    def test_podman_remote_env_resolves_remote(self, monkeypatch):
        """PODRUN_PODMAN_REMOTE=1 → resolves to podman-remote."""
        monkeypatch.setenv(ENV_PODRUN_PODMAN_REMOTE, '1')
        original_which = podrun_mod.shutil.which

        def fake_which(name):
            if name == 'podman-remote':
                return '/usr/bin/podman-remote'
            return original_which(name)

        monkeypatch.setattr(podrun_mod.shutil, 'which', fake_which)
        assert _default_podman_path() == '/usr/bin/podman-remote'

    def test_podman_remote_env_falls_back_to_podman_with_container_host(self, monkeypatch):
        """PODRUN_PODMAN_REMOTE=1 + CONTAINER_HOST + no podman-remote → podman."""
        monkeypatch.setenv(ENV_PODRUN_PODMAN_REMOTE, '1')
        monkeypatch.setenv('CONTAINER_HOST', 'unix:///run/podman/podman.sock')

        def fake_which(name):
            if name == 'podman':
                return '/usr/bin/podman'
            return None

        monkeypatch.setattr(podrun_mod.shutil, 'which', fake_which)
        assert _default_podman_path() == '/usr/bin/podman'

    def test_podman_remote_env_no_fallback_without_container_host(self, monkeypatch, capsys):
        """PODRUN_PODMAN_REMOTE=1 without CONTAINER_HOST → no podman fallback."""
        monkeypatch.setenv(ENV_PODRUN_PODMAN_REMOTE, '1')

        def fake_which(name):
            if name == 'podman':
                return '/usr/bin/podman'
            return None

        monkeypatch.setattr(podrun_mod.shutil, 'which', fake_which)
        with pytest.raises(SystemExit) as exc_info:
            _default_podman_path()
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert ENV_PODRUN_PODMAN_REMOTE in err
        assert 'podman-remote' in err

    def test_podman_remote_env_missing_all_binaries_errors(self, monkeypatch, capsys):
        """PODRUN_PODMAN_REMOTE=1 without any podman binary → hard error."""
        monkeypatch.setenv(ENV_PODRUN_PODMAN_REMOTE, '1')
        monkeypatch.setattr(podrun_mod.shutil, 'which', lambda name: None)
        with pytest.raises(SystemExit) as exc_info:
            _default_podman_path()
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert ENV_PODRUN_PODMAN_REMOTE in err

    def test_fallback_to_podman_remote(self, monkeypatch):
        """No podman found → falls back to podman-remote."""

        def fake_which(name):
            if name == 'podman-remote':
                return '/usr/bin/podman-remote'
            return None

        monkeypatch.setattr(podrun_mod.shutil, 'which', fake_which)
        assert _default_podman_path() == '/usr/bin/podman-remote'

    def test_no_binaries_returns_none(self, monkeypatch):
        """Neither podman nor podman-remote found → returns None."""
        monkeypatch.setattr(podrun_mod.shutil, 'which', lambda name: None)
        assert _default_podman_path() is None

    def test_podman_path_env_returns_resolved(self, monkeypatch, podman_binary):
        monkeypatch.setenv(ENV_PODRUN_PODMAN_PATH, podman_binary)
        path = _default_podman_path()
        assert path is not None
        assert podman_binary in path

    def test_podman_path_env_invalid_exits(self, monkeypatch):
        monkeypatch.setenv(ENV_PODRUN_PODMAN_PATH, 'no-such-binary-xyz')
        with pytest.raises(SystemExit) as exc_info:
            _default_podman_path()
        assert exc_info.value.code == 1

    def test_podman_path_env_invalid_message(self, monkeypatch, capsys):
        monkeypatch.setenv(ENV_PODRUN_PODMAN_PATH, 'no-such-binary-xyz')
        with pytest.raises(SystemExit):
            _default_podman_path()
        err = capsys.readouterr().err
        assert ENV_PODRUN_PODMAN_PATH in err
        assert 'no-such-binary-xyz' in err

    def test_podman_path_env_overrides_remote_env(self, monkeypatch, tmp_path):
        """PODRUN_PODMAN_PATH takes priority over PODRUN_PODMAN_REMOTE."""
        fake_bin = tmp_path / 'podman'
        fake_bin.write_text('#!/bin/sh\n')
        fake_bin.chmod(0o755)
        monkeypatch.setenv(ENV_PODRUN_PODMAN_REMOTE, '1')
        monkeypatch.setenv(ENV_PODRUN_PODMAN_PATH, str(fake_bin))
        path = _default_podman_path()
        assert path == str(fake_bin)
        assert 'podman-remote' not in path

    def test_podman_path_env_absolute(self, monkeypatch, tmp_path):
        """PODRUN_PODMAN_PATH works with absolute paths."""
        fake_bin = tmp_path / 'my-podman'
        fake_bin.write_text('#!/bin/sh\n')
        fake_bin.chmod(0o755)
        monkeypatch.setenv(ENV_PODRUN_PODMAN_PATH, str(fake_bin))
        path = _default_podman_path()
        assert path == str(fake_bin)


# ---------------------------------------------------------------------------
# _is_remote
# ---------------------------------------------------------------------------


class TestIsRemote:
    """Test _is_remote() — checks binary basename and CONTAINER_HOST."""

    def test_podman_remote_is_true(self):
        assert _is_remote('podman-remote') is True
        assert _is_remote('/usr/bin/podman-remote') is True

    def test_podman_is_false(self):
        assert _is_remote('podman') is False
        assert _is_remote('/usr/bin/podman') is False

    def test_custom_path_basename(self):
        assert _is_remote('/opt/custom/podman-remote') is True
        assert _is_remote('/opt/custom/podman') is False

    def test_container_host_makes_podman_remote(self, monkeypatch):
        """CONTAINER_HOST in env causes even full podman to be treated as remote."""
        monkeypatch.setenv('CONTAINER_HOST', 'unix:///run/podman/podman.sock')
        assert _is_remote('podman') is True
        assert _is_remote('/usr/bin/podman') is True

    def test_container_host_unset_podman_not_remote(self):
        """Without CONTAINER_HOST, full podman is not remote."""
        # _isolate already clears CONTAINER_HOST
        assert _is_remote('podman') is False


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
# _resolve_overlay_mounts
# ---------------------------------------------------------------------------


class TestResolveOverlayMounts:
    """Tests for fuse-overlayfs storage-opt injection and :O fallback."""

    @staticmethod
    def _ctx(ns):
        """Build a minimal PodrunContext wrapping the given ns dict."""
        return PodrunContext(
            ns=ns, trailing_args=[], explicit_command=[], raw_argv=[], subcmd_passthrough_args=[]
        )

    def test_fuse_flag_injects_storage_opt(self, monkeypatch):
        monkeypatch.setattr(
            podrun_mod.shutil,
            'which',
            lambda name: '/usr/bin/fuse-overlayfs' if name == 'fuse-overlayfs' else None,
        )
        ns = {'run.fuse_overlayfs': True, 'run.passthrough_args': ['-v=/a:/b']}
        _resolve_overlay_mounts(self._ctx(ns))
        gf = ns.get('podman_global_args') or []
        assert '--storage-opt' in gf
        assert 'overlay.mount_program=/usr/bin/fuse-overlayfs' in gf

    def test_fuse_flag_exits_when_not_found(self, monkeypatch):
        monkeypatch.setattr(podrun_mod.shutil, 'which', lambda name: None)
        ns = {'run.fuse_overlayfs': True}
        with pytest.raises(SystemExit):
            _resolve_overlay_mounts(self._ctx(ns))

    def test_fuse_flag_appends_to_existing_global_args(self, monkeypatch):
        monkeypatch.setattr(
            podrun_mod.shutil,
            'which',
            lambda name: '/usr/bin/fuse-overlayfs' if name == 'fuse-overlayfs' else None,
        )
        ns = {'run.fuse_overlayfs': True, 'podman_global_args': ['--root', '/x']}
        _resolve_overlay_mounts(self._ctx(ns))
        gf = ns['podman_global_args']
        assert '--root' in gf
        assert '--storage-opt' in gf

    def test_file_overlay_to_copy_staging_equals_form(self, tmp_path, monkeypatch):
        """File :O mounts → copy-staging (even with fuse-overlayfs available)."""
        monkeypatch.setattr(
            podrun_mod.shutil,
            'which',
            lambda name: '/usr/bin/fuse-overlayfs' if name == 'fuse-overlayfs' else None,
        )
        f = tmp_path / 'file.txt'
        f.write_text('hi')
        ns = {'run.passthrough_args': [f'-v={f}:/container/file.txt:O']}
        ctx = self._ctx(ns)
        _resolve_overlay_mounts(ctx)
        assert ns['run.passthrough_args'] == []
        assert ctx.copy_staging == [(str(f), '/container/file.txt')]

    def test_file_overlay_to_copy_staging_space_form(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            podrun_mod.shutil,
            'which',
            lambda name: '/usr/bin/fuse-overlayfs' if name == 'fuse-overlayfs' else None,
        )
        f = tmp_path / 'file.txt'
        f.write_text('hi')
        ns = {'run.passthrough_args': ['-v', f'{f}:/container/file.txt:O']}
        ctx = self._ctx(ns)
        _resolve_overlay_mounts(ctx)
        assert ns['run.passthrough_args'] == []
        assert ctx.copy_staging == [(str(f), '/container/file.txt')]

    def test_dir_overlay_preserved_with_fuse(self, tmp_path, monkeypatch):
        """Directory :O mounts stay native when fuse-overlayfs is available."""
        monkeypatch.setattr(
            podrun_mod.shutil,
            'which',
            lambda name: '/usr/bin/fuse-overlayfs' if name == 'fuse-overlayfs' else None,
        )
        d = tmp_path / 'dir'
        d.mkdir()
        ns = {'run.passthrough_args': [f'-v={d}:/container/dir:O']}
        ctx = self._ctx(ns)
        _resolve_overlay_mounts(ctx)
        assert ns['run.passthrough_args'] == [f'-v={d}:/container/dir:O']
        assert ctx.copy_staging == []

    def test_dir_overlay_preserved_with_fuse_space_form(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            podrun_mod.shutil,
            'which',
            lambda name: '/usr/bin/fuse-overlayfs' if name == 'fuse-overlayfs' else None,
        )
        d = tmp_path / 'dir'
        d.mkdir()
        ns = {'run.passthrough_args': ['-v', f'{d}:/container/dir:O']}
        ctx = self._ctx(ns)
        _resolve_overlay_mounts(ctx)
        assert ns['run.passthrough_args'] == ['-v', f'{d}:/container/dir:O']
        assert ctx.copy_staging == []

    def test_dir_overlay_fallback_without_fuse(self, tmp_path, monkeypatch):
        """Directory :O mounts → copy-staging when fuse-overlayfs absent."""
        monkeypatch.setattr(podrun_mod.shutil, 'which', lambda name: None)
        d = tmp_path / 'dir'
        d.mkdir()
        ns = {'run.passthrough_args': [f'-v={d}:/container/dir:O']}
        ctx = self._ctx(ns)
        _resolve_overlay_mounts(ctx)
        assert ns['run.passthrough_args'] == []
        assert ctx.copy_staging == [(str(d), '/container/dir')]

    def test_dir_overlay_fallback_without_fuse_space_form(self, tmp_path, monkeypatch):
        monkeypatch.setattr(podrun_mod.shutil, 'which', lambda name: None)
        d = tmp_path / 'dir'
        d.mkdir()
        ns = {'run.passthrough_args': ['-v', f'{d}:/container/dir:O']}
        ctx = self._ctx(ns)
        _resolve_overlay_mounts(ctx)
        assert ns['run.passthrough_args'] == []
        assert ctx.copy_staging == [(str(d), '/container/dir')]

    def test_non_volume_args_unchanged(self, monkeypatch):
        monkeypatch.setattr(podrun_mod.shutil, 'which', lambda name: None)
        ns = {'run.passthrough_args': ['--rm', '-e', 'FOO=bar', '-it']}
        ctx = self._ctx(ns)
        _resolve_overlay_mounts(ctx)
        assert ns['run.passthrough_args'] == ['--rm', '-e', 'FOO=bar', '-it']
        assert ctx.copy_staging == []

    def test_empty_passthrough(self, monkeypatch):
        monkeypatch.setattr(podrun_mod.shutil, 'which', lambda name: None)
        ns = {}
        ctx = self._ctx(ns)
        _resolve_overlay_mounts(ctx)
        assert ns['run.passthrough_args'] == []
        assert ctx.copy_staging == []

    def test_no_fuse_flag_skips_storage_opt(self, monkeypatch):
        """Without --fuse-overlayfs, no storage-opt is injected."""
        monkeypatch.setattr(
            podrun_mod.shutil,
            'which',
            lambda name: '/usr/bin/fuse-overlayfs' if name == 'fuse-overlayfs' else None,
        )
        ns = {'run.passthrough_args': ['-v=/a:/b']}
        _resolve_overlay_mounts(self._ctx(ns))
        gf = ns.get('podman_global_args') or []
        assert '--storage-opt' not in gf

    def test_mixed_mounts(self, tmp_path, monkeypatch):
        """Mix of file :O, dir :O, and normal mounts."""
        monkeypatch.setattr(podrun_mod.shutil, 'which', lambda name: None)
        f = tmp_path / 'file.txt'
        f.write_text('hi')
        d = tmp_path / 'dir'
        d.mkdir()
        ns = {
            'run.passthrough_args': [
                f'-v={f}:/container/file.txt:O',
                f'-v={d}:/container/dir:O',
                '-v=/normal:/mount',
            ]
        }
        ctx = self._ctx(ns)
        _resolve_overlay_mounts(ctx)
        assert ns['run.passthrough_args'] == ['-v=/normal:/mount']
        assert len(ctx.copy_staging) == 2
        assert (str(f), '/container/file.txt') in ctx.copy_staging
        assert (str(d), '/container/dir') in ctx.copy_staging


# ---------------------------------------------------------------------------
# _handle_run — through main() with --print-cmd
# ---------------------------------------------------------------------------


class TestHandleRunViaPrintCmd:
    """Test _handle_run indirectly via main(['--print-cmd', 'run', ...])."""

    @pytest.fixture(autouse=True)
    def _mock_run_os_cmd(self, monkeypatch):
        """Suppress stale file cleanup and container state checks."""
        monkeypatch.setattr(
            podrun_mod,
            'run_os_cmd',
            lambda cmd, env=None: subprocess.CompletedProcess(
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

    def test_restart_prints_start_attach(self, monkeypatch, capsys):
        """When restarting, --print-cmd should show start -a -i command (session is interactive)."""
        monkeypatch.setattr(podrun_mod, 'detect_container_state', lambda *a, **kw: 'stopped')
        with pytest.raises(SystemExit) as exc_info:
            main(['--print-cmd', 'run', '--name=myc', '--auto-attach', '--session', 'alpine'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        cmd = shlex.split(out.strip())
        assert 'start' in cmd
        assert '-a' in cmd
        assert '-i' in cmd
        assert 'myc' in cmd

    def test_restart_non_interactive_no_stdin(self, monkeypatch, capsys):
        """When restarting without interactive overlay or -i, -i is not passed."""
        monkeypatch.setattr(podrun_mod, 'detect_container_state', lambda *a, **kw: 'stopped')
        with pytest.raises(SystemExit) as exc_info:
            main(['--print-cmd', 'run', '--name=myc', '--auto-attach', 'alpine'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        cmd = shlex.split(out.strip())
        assert 'start' in cmd
        assert '-a' in cmd
        assert '-i' not in cmd
        assert '-ai' not in cmd

    def test_restart_passthrough_interactive(self, monkeypatch, capsys):
        """When restarting with -i via passthrough (no overlay), -i is passed."""
        monkeypatch.setattr(podrun_mod, 'detect_container_state', lambda *a, **kw: 'stopped')
        with pytest.raises(SystemExit) as exc_info:
            main(['--print-cmd', 'run', '--name=myc', '--auto-attach', '-i', 'alpine'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        cmd = shlex.split(out.strip())
        assert 'start' in cmd
        assert '-a' in cmd
        assert '-i' in cmd

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
    def _mock_run_os_cmd(self, monkeypatch):
        """Suppress stale file cleanup and container state checks."""
        monkeypatch.setattr(
            podrun_mod,
            'run_os_cmd',
            lambda cmd, env=None: subprocess.CompletedProcess(
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


@pytest.mark.usefixtures('requires_podman_remote')
class TestRemotePodrunExecution:
    """Podrun with podman-remote should work for normal operations."""

    def _force_remote(self, monkeypatch):
        """Force remote mode by patching _default_podman_path directly."""
        path = shutil.which('podman-remote')
        monkeypatch.setattr(podrun_mod, '_default_podman_path', lambda: path)
        monkeypatch.setenv(ENV_PODRUN_PODMAN_REMOTE, '1')

    def test_version_works_when_remote(self, monkeypatch):
        self._force_remote(monkeypatch)
        with pytest.raises(SystemExit) as exc_info:
            main(['--version'])
        assert exc_info.value.code == 0

    def test_passthrough_proceeds_when_remote(self, monkeypatch, capsys):
        self._force_remote(monkeypatch)
        with pytest.raises(SystemExit) as exc_info:
            main(['--print-cmd', 'ps', '-a'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'ps' in out

    def test_run_proceeds_when_remote(self, monkeypatch, capsys, tmp_path):
        self._force_remote(monkeypatch)
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

    def test_local_store_destroy_errors_when_remote(self, monkeypatch, capsys):
        self._force_remote(monkeypatch)
        with pytest.raises(SystemExit) as exc_info:
            main(['--local-store', '/tmp/s', '--local-store-destroy'])
        assert exc_info.value.code == 1
        assert 'not supported' in capsys.readouterr().err

    def test_version_works_without_remote(self, monkeypatch):
        # autouse fixture clears PODRUN_PODMAN_REMOTE — uses regular podman
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
            return podrun_mod.shutil.which('podman-remote') or podrun_mod.shutil.which('podman')

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
# Remote guard behavior in _handle_run
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures('requires_podman_remote')
class TestRemoteHandleRunGuards:
    """Verify _warn_missing_subids and _resolve_overlay_mounts are skipped when remote."""

    @pytest.fixture(autouse=True)
    def _mock_run_os_cmd(self, monkeypatch):
        """Suppress stale file cleanup and container state checks."""
        monkeypatch.setattr(
            podrun_mod,
            'run_os_cmd',
            lambda cmd, env=None: subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout='', stderr=''
            ),
        )

    def _force_remote(self, monkeypatch):
        """Force remote mode by patching _default_podman_path directly."""
        path = shutil.which('podman-remote')
        monkeypatch.setattr(podrun_mod, '_default_podman_path', lambda: path)
        monkeypatch.setenv(ENV_PODRUN_PODMAN_REMOTE, '1')

    def test_warn_subids_skipped_when_remote(self, monkeypatch, capsys):
        self._force_remote(monkeypatch)
        with pytest.raises(SystemExit) as exc_info:
            main(['--print-cmd', 'run', '--session', 'alpine'])
        assert exc_info.value.code == 0
        err = capsys.readouterr().err
        # Should not contain subuid/subgid warnings when remote
        assert 'subuid' not in err.lower()

    def test_fuse_fixup_skipped_when_remote(self, monkeypatch, capsys):
        self._force_remote(monkeypatch)
        with pytest.raises(SystemExit) as exc_info:
            main(['--print-cmd', 'run', '--fuse-overlayfs', 'alpine'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        # --storage-opt should NOT be injected when remote
        assert '--storage-opt' not in out


# ---------------------------------------------------------------------------
# _discover_podrunrc + ~/.podrunrc* integration
# ---------------------------------------------------------------------------


class TestDiscoverPodrunrc:
    """Unit tests for _discover_podrunrc() discovery logic."""

    def test_no_matches_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        assert _discover_podrunrc() is None

    def test_single_match_returns_path(self, tmp_path, monkeypatch):
        rc = tmp_path / '.podrunrc'
        rc.write_text('--session\n')
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        assert _discover_podrunrc() == str(rc)

    def test_dotsh_extension(self, tmp_path, monkeypatch):
        rc = tmp_path / '.podrunrc.sh'
        rc.write_text('echo --session\n')
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        assert _discover_podrunrc() == str(rc)

    def test_dotpy_extension(self, tmp_path, monkeypatch):
        rc = tmp_path / '.podrunrc.py'
        rc.write_text('print("--session")\n')
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        assert _discover_podrunrc() == str(rc)

    def test_directories_ignored(self, tmp_path, monkeypatch):
        (tmp_path / '.podrunrc_dir').mkdir()
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        assert _discover_podrunrc() is None

    def test_directory_plus_file(self, tmp_path, monkeypatch):
        """Directory .podrunrc_dir is ignored; only .podrunrc file is returned."""
        (tmp_path / '.podrunrc_dir').mkdir()
        rc = tmp_path / '.podrunrc'
        rc.write_text('--session\n')
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        assert _discover_podrunrc() == str(rc)

    def test_multiple_matches_exits(self, tmp_path, monkeypatch, capsys):
        (tmp_path / '.podrunrc').write_text('')
        (tmp_path / '.podrunrc.sh').write_text('')
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        with pytest.raises(SystemExit) as exc_info:
            _discover_podrunrc()
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert '.podrunrc' in err
        assert '.podrunrc.sh' in err


class TestPodrunrcIntegration:
    """Integration tests for ~/.podrunrc* execution and merge precedence."""

    @pytest.fixture(autouse=True)
    def _mock_run_os_cmd(self, monkeypatch):
        """Suppress stale file cleanup and container state checks."""
        monkeypatch.setattr(
            podrun_mod,
            'run_os_cmd',
            lambda cmd, env=None: subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='',
                stderr='',
            ),
        )

    def _write_rc(self, tmp_path, output):
        """Write a podrunrc Python script that prints *output*."""
        rc = tmp_path / '.podrunrc'
        rc.write_text(f'print({output!r})')
        return str(rc)

    def _run(self, argv, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(['--print-cmd'] + argv)
        assert exc_info.value.code == 0
        return shlex.split(capsys.readouterr().out)

    def test_rc_flags_appear_in_command(self, tmp_path, monkeypatch, capsys):
        """~/.podrunrc output is parsed and appears in the podman command."""
        rc = self._write_rc(tmp_path, '--session')
        monkeypatch.setattr(podrun_mod, '_discover_podrunrc', lambda: rc)
        cmd = self._run(['run', 'alpine'], capsys)
        # --session implies -it and --userns=keep-id
        assert '-it' in cmd
        assert '--userns=keep-id' in cmd

    def test_rc_passthrough_flags_in_command(self, tmp_path, monkeypatch, capsys):
        """Passthrough flags from rc appear in the podman command."""
        rc = self._write_rc(tmp_path, '-e RC_VAR=1')
        monkeypatch.setattr(podrun_mod, '_discover_podrunrc', lambda: rc)
        cmd = self._run(['run', 'alpine'], capsys)
        assert '-e' in cmd
        assert 'RC_VAR=1' in cmd

    def test_cli_overrides_rc(self, tmp_path, monkeypatch, capsys):
        """CLI flags take precedence over rc flags."""
        rc = self._write_rc(tmp_path, '--shell /bin/bash')
        monkeypatch.setattr(podrun_mod, '_discover_podrunrc', lambda: rc)
        cmd = self._run(['run', '--shell', '/bin/zsh', '--session', 'alpine'], capsys)
        # The CLI --shell /bin/zsh should win over rc's --shell /bin/bash
        # We can't directly see the shell in the command, but the entrypoint
        # script is generated with it. Verify via PODRUN_SHELL env.
        env_args = [a for a in cmd if 'PODRUN_SHELL' in a]
        assert any('/bin/zsh' in a for a in env_args)

    def test_dc_overrides_rc(self, tmp_path, monkeypatch, capsys):
        """devcontainer.json overrides rc for scalar fields."""
        dc_path = tmp_path / 'devcontainer.json'
        dc_path.write_text('{"customizations": {"podrun": {"name": "dc-name"}}}')
        monkeypatch.setattr(podrun_mod, 'find_devcontainer_json', lambda start_dir=None: dc_path)
        rc = self._write_rc(tmp_path, '--name rc-name')
        monkeypatch.setattr(podrun_mod, '_discover_podrunrc', lambda: rc)
        cmd = self._run(['run', '--session', 'alpine'], capsys)
        assert '--name=dc-name' in cmd

    def test_no_podrunrc_flag_skips_discovery(self, monkeypatch, capsys):
        """--no-podrunrc prevents rc discovery and execution."""
        discovered = []
        monkeypatch.setattr(
            podrun_mod,
            '_discover_podrunrc',
            lambda: discovered.append(1) or '/fake/.podrunrc',
        )
        self._run(['--no-podrunrc', 'run', 'alpine'], capsys)
        assert len(discovered) == 0

    def test_dc_nopodrunrc_skips_discovery(self, tmp_path, monkeypatch, capsys):
        """noPodrunrc in devcontainer.json prevents rc discovery."""
        dc_path = tmp_path / 'devcontainer.json'
        dc_path.write_text('{"customizations": {"podrun": {"noPodrunrc": true}}}')
        monkeypatch.setattr(podrun_mod, 'find_devcontainer_json', lambda start_dir=None: dc_path)
        discovered = []
        monkeypatch.setattr(
            podrun_mod,
            '_discover_podrunrc',
            lambda: discovered.append(1) or '/fake/.podrunrc',
        )
        self._run(['run', 'alpine'], capsys)
        assert len(discovered) == 0

    def test_no_rc_file_no_effect(self, monkeypatch, capsys):
        """When _discover_podrunrc returns None, no error and command works."""
        monkeypatch.setattr(podrun_mod, '_discover_podrunrc', lambda: None)
        cmd = self._run(['run', 'alpine'], capsys)
        assert 'run' in cmd
        assert 'alpine' in cmd

    def test_rc_export_lowest_priority(self, tmp_path, monkeypatch, capsys):
        """rc exports appear before dc/script/cli exports in combined list."""
        rc_dst = tmp_path / 'rc_dst'
        dc_dst = tmp_path / 'dc_dst'
        cli_dst = tmp_path / 'cli_dst'
        rc_dst.mkdir()
        dc_dst.mkdir()
        cli_dst.mkdir()

        dc_path = tmp_path / 'devcontainer.json'
        dc_path.write_text(
            f'{{"customizations": {{"podrun": {{"exports": ["/dc_src:{dc_dst}"]}}}}}}'
        )
        monkeypatch.setattr(podrun_mod, 'find_devcontainer_json', lambda start_dir=None: dc_path)
        rc = self._write_rc(tmp_path, f'--export /rc_src:{rc_dst}')
        monkeypatch.setattr(podrun_mod, '_discover_podrunrc', lambda: rc)
        cmd = self._run(
            ['run', '--user-overlay', '--export', f'/cli_src:{cli_dst}', 'alpine'],
            capsys,
        )
        # All three export volumes should be present
        joined = ' '.join(cmd)
        assert str(rc_dst) in joined
        assert str(dc_dst) in joined
        assert str(cli_dst) in joined

    def test_rc_passthrough_lowest_priority(self, tmp_path, monkeypatch, capsys):
        """rc passthrough args come before dc/script/cli passthrough."""
        rc = self._write_rc(tmp_path, '-e FROM_RC=1')
        monkeypatch.setattr(podrun_mod, '_discover_podrunrc', lambda: rc)
        cmd = self._run(['run', '-e', 'FROM_CLI=1', 'alpine'], capsys)
        # Both env vars should be present
        joined = ' '.join(cmd)
        assert 'FROM_RC=1' in joined
        assert 'FROM_CLI=1' in joined
        # RC passthrough should appear before CLI passthrough
        rc_idx = next(i for i, a in enumerate(cmd) if a == 'FROM_RC=1')
        cli_idx = next(i for i, a in enumerate(cmd) if a == 'FROM_CLI=1')
        assert rc_idx < cli_idx


# ---------------------------------------------------------------------------
# _cleanup
# ---------------------------------------------------------------------------


class TestCleanup:
    def test_cleanup_removes_tmp(self, tmp_path, capsys):
        """_cleanup removes PODRUN_TMP directory."""
        # _isolate already redirects PODRUN_TMP to tmp_path.
        script = tmp_path / 'test-entrypoint.sh'
        script.write_text('#!/bin/sh\n')
        assert tmp_path.exists()
        _cleanup()
        assert not tmp_path.exists()
        err = capsys.readouterr().err
        assert str(tmp_path) in err
        assert 'Entrypoint scripts and staging' in err
        assert 'All cleaned up.' in err

    def test_cleanup_removes_cache(self, tmp_path, monkeypatch, capsys):
        """_cleanup removes flags cache directory."""
        cache_dir = tmp_path / 'fake-cache'
        cache_dir.mkdir()
        (cache_dir / 'podman-5.4.0.json').write_text('{}')
        monkeypatch.setattr(podrun_mod, '_flags_cache_dir', lambda: str(cache_dir))
        # Ensure PODRUN_TMP doesn't exist so it doesn't confuse output.
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path / 'nonexistent'))
        _cleanup()
        assert not cache_dir.exists()
        err = capsys.readouterr().err
        assert str(cache_dir) in err
        assert 'Podman flags cache' in err

    def test_cleanup_removes_stores_dir(self, tmp_path, monkeypatch, capsys):
        """_cleanup removes idle store service entries."""
        stores = tmp_path / 'stores'
        stores.mkdir()
        entry = stores / 'abc123'
        entry.mkdir()
        # PID file with a dead PID — os.kill will raise OSError.
        (entry / 'podman.pid').write_text('999999999')
        monkeypatch.setattr(podrun_mod, '_PODRUN_STORES_DIR', str(stores))
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path / 'nonexistent'))
        monkeypatch.setattr(podrun_mod, '_flags_cache_dir', lambda: str(tmp_path / 'nonexistent2'))
        _cleanup()
        assert not stores.exists()
        err = capsys.readouterr().err
        assert str(stores) in err
        assert 'Store service runtime' in err

    def test_cleanup_stops_idle_service(self, tmp_path, monkeypatch, capsys):
        """_cleanup SIGTERMs idle services (no containers) and removes dirs."""
        import os
        import signal

        stores = tmp_path / 'stores'
        stores.mkdir()
        entry = stores / 'idle'
        entry.mkdir()
        # Socket exists but no containers running.
        (entry / 'podman.sock').write_text('')
        fake_pid = os.getpid()
        (entry / 'podman.pid').write_text(str(fake_pid))
        kills = []
        monkeypatch.setattr(os, 'kill', lambda pid, sig: kills.append((pid, sig)))
        # Mock subprocess.run — ps -q returns empty (no containers).
        monkeypatch.setattr(
            podrun_mod.subprocess,
            'run',
            lambda *a, **kw: subprocess.CompletedProcess(
                args=a, returncode=0, stdout='', stderr=''
            ),
        )
        monkeypatch.setattr(podrun_mod, '_PODRUN_STORES_DIR', str(stores))
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path / 'nonexistent'))
        monkeypatch.setattr(podrun_mod, '_flags_cache_dir', lambda: str(tmp_path / 'nonexistent2'))
        _cleanup()
        # Service should have been sent SIGTERM.
        assert (fake_pid, signal.SIGTERM) in kills
        # Entry and parent should be removed.
        assert not entry.exists()
        assert not stores.exists()
        err = capsys.readouterr().err
        assert f'Stopped: Store service (PID {fake_pid})' in err

    def test_cleanup_skips_active_containers_via_socket(self, tmp_path, monkeypatch, capsys):
        """_cleanup skips store entries with containers (socket query)."""
        stores = tmp_path / 'stores'
        stores.mkdir()
        entry = stores / 'active'
        entry.mkdir()
        (entry / 'podman.sock').write_text('')
        (entry / 'podman.pid').write_text('12345')
        # Mock subprocess.run — ps -qa returns a container ID.
        monkeypatch.setattr(
            podrun_mod.subprocess,
            'run',
            lambda *a, **kw: subprocess.CompletedProcess(
                args=a, returncode=0, stdout='abc123\n', stderr=''
            ),
        )
        monkeypatch.setattr(podrun_mod, '_PODRUN_STORES_DIR', str(stores))
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path / 'nonexistent'))
        monkeypatch.setattr(podrun_mod, '_flags_cache_dir', lambda: str(tmp_path / 'nonexistent2'))
        _cleanup()
        assert entry.exists()
        err = capsys.readouterr().err
        assert 'Skipped: Store service entry (containers exist)' in err

    def test_cleanup_skips_stopped_containers_no_socket(self, tmp_path, monkeypatch, capsys):
        """_cleanup skips store entries with overlay-containers/ with config.json on disk."""
        stores = tmp_path / 'stores'
        stores.mkdir()
        entry = stores / 'stopped'
        entry.mkdir()
        # No socket, no PID — service is down.  But container data exists
        # with a valid config.json (real container, not stale metadata).
        userdata = entry / 'overlay-containers' / 'abc123' / 'userdata'
        userdata.mkdir(parents=True)
        (userdata / 'config.json').write_text('{}')
        monkeypatch.setattr(podrun_mod, '_PODRUN_STORES_DIR', str(stores))
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path / 'nonexistent'))
        monkeypatch.setattr(podrun_mod, '_flags_cache_dir', lambda: str(tmp_path / 'nonexistent2'))
        _cleanup()
        assert entry.exists()
        err = capsys.readouterr().err
        assert 'Skipped: Store service entry (containers exist)' in err

    def test_cleanup_removes_stale_container_dirs(self, tmp_path, monkeypatch, capsys):
        """_cleanup removes entries with overlay-containers/ but no config.json (stale)."""
        stores = tmp_path / 'stores'
        stores.mkdir()
        entry = stores / 'stale'
        entry.mkdir()
        # Bare container dir without userdata/config.json — stale metadata.
        containers = entry / 'overlay-containers' / 'abc123'
        containers.mkdir(parents=True)
        monkeypatch.setattr(podrun_mod, '_PODRUN_STORES_DIR', str(stores))
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path / 'nonexistent'))
        monkeypatch.setattr(podrun_mod, '_flags_cache_dir', lambda: str(tmp_path / 'nonexistent2'))
        _cleanup()
        assert not entry.exists()
        err = capsys.readouterr().err
        assert 'Skipped' not in err

    def test_cleanup_unshare_fallback(self, tmp_path, monkeypatch, capsys):
        """_cleanup falls back to podman unshare rm -rf on PermissionError."""
        stores = tmp_path / 'stores'
        stores.mkdir()
        entry = stores / 'mapped'
        entry.mkdir()
        (entry / 'podman.pid').write_text('999999999')

        real_rmtree = shutil.rmtree
        first_call = [True]

        def fake_rmtree(path, **kw):
            # First call (on entry) raises PermissionError; later calls succeed.
            if first_call[0] and str(path) == str(entry):
                first_call[0] = False
                raise PermissionError(13, 'Permission denied', str(path))
            real_rmtree(path, **kw)

        monkeypatch.setattr(shutil, 'rmtree', fake_rmtree)

        # Mock subprocess.run to simulate `podman unshare rm -rf` success.
        def fake_run(*args, **kw):
            cmd = args[0] if args else kw.get('args', [])
            if 'unshare' in cmd:
                real_rmtree(str(entry), ignore_errors=True)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout='', stderr='')

        monkeypatch.setattr(podrun_mod.subprocess, 'run', fake_run)
        monkeypatch.setattr(podrun_mod, '_PODRUN_STORES_DIR', str(stores))
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path / 'nonexistent'))
        monkeypatch.setattr(podrun_mod, '_flags_cache_dir', lambda: str(tmp_path / 'nonexistent2'))
        _cleanup()
        # Entry should be gone after unshare fallback.
        assert not entry.exists()
        err = capsys.readouterr().err
        assert 'Store service runtime' in err
        assert 'Failed' not in err

    def test_cleanup_unshare_partial_then_retry(self, tmp_path, monkeypatch, capsys):
        """_cleanup retries rmtree after podman unshare partially cleans."""
        stores = tmp_path / 'stores'
        stores.mkdir()
        entry = stores / 'partial'
        entry.mkdir()
        mapped_file = entry / 'uid-mapped'
        mapped_file.write_text('data')

        real_rmtree = shutil.rmtree
        call_count = [0]

        def fake_rmtree(path, **kw):
            call_count[0] += 1
            if call_count[0] == 1 and str(path) == str(entry):
                # First call: PermissionError (simulating UID-mapped files)
                raise PermissionError(13, 'Permission denied', str(path))
            # Second call (retry after unshare): succeeds
            real_rmtree(path, **kw)

        monkeypatch.setattr(shutil, 'rmtree', fake_rmtree)

        # Mock unshare: removes the UID-mapped file but leaves the directory
        def fake_run(*args, **kw):
            cmd = args[0] if args else kw.get('args', [])
            if 'unshare' in cmd:
                mapped_file.unlink(missing_ok=True)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout='', stderr='')

        monkeypatch.setattr(podrun_mod.subprocess, 'run', fake_run)
        monkeypatch.setattr(podrun_mod, '_PODRUN_STORES_DIR', str(stores))
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path / 'nonexistent'))
        monkeypatch.setattr(podrun_mod, '_flags_cache_dir', lambda: str(tmp_path / 'nonexistent2'))
        _cleanup()
        assert not entry.exists()
        err = capsys.readouterr().err
        assert 'Failed' not in err

    def test_cleanup_rmtree_race_retry(self, tmp_path, monkeypatch, capsys):
        """_cleanup retries rmtree when OSError occurs (file vanished mid-walk)."""
        stores = tmp_path / 'stores'
        stores.mkdir()
        entry = stores / 'racy'
        entry.mkdir()
        monkeypatch.setattr(podrun_mod, '_PODRUN_STORES_DIR', str(stores))
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path / 'nonexistent'))
        monkeypatch.setattr(podrun_mod, '_flags_cache_dir', lambda: str(tmp_path / 'nonexistent2'))
        # First rmtree raises OSError (simulating file vanished mid-walk),
        # second call succeeds (real rmtree).
        real_rmtree = shutil.rmtree
        call_count = [0]

        def racy_rmtree(path, **kw):
            if str(path) == str(entry) and call_count[0] == 0:
                call_count[0] += 1
                raise OSError(2, 'No such file or directory', 'podman.sock')
            return real_rmtree(path, **kw)

        monkeypatch.setattr(shutil, 'rmtree', racy_rmtree)
        _cleanup()
        assert not entry.exists()
        err = capsys.readouterr().err
        assert 'Error removing' not in err

    def test_cleanup_noop_when_empty(self, tmp_path, monkeypatch, capsys):
        """_cleanup prints 'Nothing to clean.' when no dirs exist."""
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path / 'nonexistent'))
        monkeypatch.setattr(podrun_mod, '_PODRUN_STORES_DIR', str(tmp_path / 'nonexistent2'))
        monkeypatch.setattr(podrun_mod, '_flags_cache_dir', lambda: str(tmp_path / 'nonexistent3'))
        _cleanup()
        err = capsys.readouterr().err
        assert 'Nothing to clean.' in err

    def test_cleanup_reports_failure(self, tmp_path, monkeypatch, capsys):
        """_cleanup prints error and 'Failed to remove' when rmtree fails."""
        target = tmp_path / 'stubborn'
        target.mkdir()
        (target / 'file').write_text('data')
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(target))
        monkeypatch.setattr(podrun_mod, '_PODRUN_STORES_DIR', str(tmp_path / 'nonexistent'))
        monkeypatch.setattr(podrun_mod, '_flags_cache_dir', lambda: str(tmp_path / 'nonexistent2'))

        # Raise PermissionError so the directory persists.
        def fake_rmtree(path, **kw):
            raise PermissionError(13, 'Permission denied', path)

        monkeypatch.setattr(shutil, 'rmtree', fake_rmtree)
        _cleanup()
        err = capsys.readouterr().err
        assert 'Error removing' in err
        assert 'Permission denied' in err
        assert 'Failed to remove' in err
        assert str(target) in err
        assert 'All cleaned up.' not in err

    def test_cleanup_flag_hidden(self, capsys):
        """--__cleanup__ does not appear in --help output."""
        from podrun.podrun import build_root_parser, load_podman_flags

        flags = load_podman_flags()
        parser = build_root_parser(flags)
        help_text = parser.format_help()
        assert '__cleanup__' not in help_text


# ---------------------------------------------------------------------------
# Stat-based flag cache
# ---------------------------------------------------------------------------


class TestStatBasedCache:
    """Verify stat-based cache key eliminates podman --version subprocess."""

    def test_warm_cache_no_subprocess(self, tmp_path, monkeypatch, podman_path):
        """Seeded cache → load_podman_flags never calls subprocess."""
        # Redirect cache dir to tmp so we don't pollute the real cache.
        monkeypatch.setattr(podrun_mod, '_flags_cache_dir', lambda: str(tmp_path))
        cache_path = _flags_cache_path(podman_path)
        flags = PodmanFlags(
            global_value_flags=frozenset(['--log-level']),
            global_boolean_flags=frozenset(),
            subcommands=frozenset(['ps', 'run']),
            run_value_flags=frozenset(['-e']),
            run_boolean_flags=frozenset(['--rm']),
        )
        _write_flags_cache(cache_path, flags)

        # Clear in-memory cache so it must hit disk.
        saved = podrun_mod._loaded_flags.copy()
        podrun_mod._loaded_flags.clear()
        try:
            # Make subprocess.run blow up if called.
            monkeypatch.setattr(
                subprocess,
                'run',
                lambda *a, **kw: (_ for _ in ()).throw(AssertionError('subprocess called')),
            )
            result = load_podman_flags(podman_path)
            assert result.subcommands == frozenset(['ps', 'run'])
        finally:
            podrun_mod._loaded_flags.clear()
            podrun_mod._loaded_flags.update(saved)

    def test_cache_miss_scrapes(self, tmp_path, monkeypatch, podman_path):
        """No cache file → live scrape occurs."""
        # Point cache dir to empty tmp dir so there's no cache hit.
        monkeypatch.setattr(podrun_mod, '_flags_cache_dir', lambda: str(tmp_path))

        scraped = False

        orig_scrape = podrun_mod._scrape_all_flags

        def tracking_scrape(pp):
            nonlocal scraped
            scraped = True
            return orig_scrape(pp)

        monkeypatch.setattr(podrun_mod, '_scrape_all_flags', tracking_scrape)

        saved = podrun_mod._loaded_flags.copy()
        podrun_mod._loaded_flags.clear()
        try:
            load_podman_flags(podman_path)
            assert scraped
        finally:
            podrun_mod._loaded_flags.clear()
            podrun_mod._loaded_flags.update(saved)

    def test_stat_change_invalidates_cache(self, tmp_path, monkeypatch):
        """Changing binary size produces a different cache path."""
        fake = tmp_path / 'podman'
        fake.write_text('v1')
        path1 = _flags_cache_path(str(fake))

        fake.write_text('v1-upgraded-binary')
        path2 = _flags_cache_path(str(fake))

        assert path1 != path2

    def test_stale_cache_cleanup_same_label(self, tmp_path):
        """Cleanup removes old files with the same label only."""
        cache_dir = tmp_path / 'cache'
        cache_dir.mkdir()

        # Create stale podman file and a podman-remote file.
        (cache_dir / 'podman-111-222.json').write_text('{}')
        (cache_dir / 'podman-remote-333-444.json').write_text('{}')
        # Non-json file should be left alone.
        (cache_dir / 'README').write_text('keep')

        current = str(cache_dir / 'podman-555-666.json')
        with open(current, 'w') as f:
            f.write('{}')

        _clean_stale_cache(current)

        remaining = sorted(os.listdir(str(cache_dir)))
        # Old podman file removed, podman-remote file preserved.
        assert remaining == ['README', 'podman-555-666.json', 'podman-remote-333-444.json']

    def test_stale_cache_cleanup_remote_label(self, tmp_path):
        """Cleanup for podman-remote does not remove podman files."""
        cache_dir = tmp_path / 'cache'
        cache_dir.mkdir()

        (cache_dir / 'podman-111-222.json').write_text('{}')
        (cache_dir / 'podman-remote-333-444.json').write_text('{}')

        current = str(cache_dir / 'podman-remote-555-666.json')
        with open(current, 'w') as f:
            f.write('{}')

        _clean_stale_cache(current)

        remaining = sorted(os.listdir(str(cache_dir)))
        # Old podman-remote file removed, podman file preserved.
        assert remaining == ['podman-111-222.json', 'podman-remote-555-666.json']

    def test_main_no_version_guard(self, monkeypatch, podman_path):
        """main() does not call get_podman_version (function removed)."""
        assert not hasattr(podrun_mod, 'get_podman_version')


# ---------------------------------------------------------------------------
# NFS remediation
# ---------------------------------------------------------------------------


class TestNfsRemediate:
    """Tests for _nfs_remediate and _is_network_fs."""

    @staticmethod
    def _ctx(ns, podman_path='podman'):
        ctx = PodrunContext(
            ns=ns,
            trailing_args=[],
            explicit_command=[],
            raw_argv=[],
            subcmd_passthrough_args=[],
        )
        ctx.podman_path = podman_path
        return ctx

    # -- Flag parsing ---------------------------------------------------------

    def test_flag_error(self):
        ctx = parse_args(['--nfs-remediate', 'error', 'version'])
        assert ctx.ns['root.nfs_remediate'] == 'error'

    def test_flag_init(self):
        ctx = parse_args(['--nfs-remediate', 'init', 'version'])
        assert ctx.ns['root.nfs_remediate'] == 'init'

    def test_flag_mv(self):
        ctx = parse_args(['--nfs-remediate=mv', 'version'])
        assert ctx.ns['root.nfs_remediate'] == 'mv'

    def test_flag_rm(self):
        ctx = parse_args(['--nfs-remediate', 'rm', 'version'])
        assert ctx.ns['root.nfs_remediate'] == 'rm'

    def test_flag_prompt(self):
        ctx = parse_args(['--nfs-remediate', 'prompt', 'version'])
        assert ctx.ns['root.nfs_remediate'] == 'prompt'

    def test_flag_absent_defaults_to_init(self):
        """Absent flag → None at parse time, 'init' at runtime."""
        ctx = parse_args(['version'])
        assert ctx.ns['root.nfs_remediate'] is None
        # _nfs_remediate treats None as 'init' via `or 'init'` fallback

    def test_flag_custom_path(self):
        ctx = parse_args(['--nfs-remediate-path', '/scratch', '--nfs-remediate', 'init', 'version'])
        assert ctx.ns['root.nfs_remediate_path'] == '/scratch'

    def test_flag_invalid_choice(self):
        with pytest.raises(SystemExit):
            parse_args(['--nfs-remediate', 'bogus', 'version'])

    # -- DC config mapping ----------------------------------------------------

    def test_dc_config_nfs_remediate(self, tmp_path, monkeypatch):
        dc_path = tmp_path / 'devcontainer.json'
        dc_path.write_text('{"customizations": {"podrun": {"nfsRemediate": "init"}}}')
        monkeypatch.setattr(
            podrun_mod, 'find_devcontainer_json', lambda start_dir=None: str(dc_path)
        )
        ctx = parse_args(['version'])
        from podrun.podrun import resolve_config

        ctx = resolve_config(ctx)
        assert ctx.ns.get('root.nfs_remediate') == 'init'

    def test_dc_config_nfs_remediate_path(self, tmp_path, monkeypatch):
        dc_path = tmp_path / 'devcontainer.json'
        dc_path.write_text('{"customizations": {"podrun": {"nfsRemediatePath": "/scratch"}}}')
        monkeypatch.setattr(
            podrun_mod, 'find_devcontainer_json', lambda start_dir=None: str(dc_path)
        )
        ctx = parse_args(['version'])
        from podrun.podrun import resolve_config

        ctx = resolve_config(ctx)
        assert ctx.ns.get('root.nfs_remediate_path') == '/scratch'

    # -- Skip conditions ------------------------------------------------------

    def test_skip_when_remote(self, monkeypatch):
        """No-op when using podman-remote."""
        ns = {'root.nfs_remediate': 'init'}
        _nfs_remediate(self._ctx(ns, podman_path='podman-remote'))
        # Should return without error (remote skips remediation)

    def test_skip_when_nested(self, monkeypatch):
        """No-op when inside a podrun container."""
        monkeypatch.setenv(ENV_PODRUN_CONTAINER, '1')
        ns = {'root.nfs_remediate': 'init'}
        _nfs_remediate(self._ctx(ns))

    def test_skip_when_not_network_fs(self, tmp_path, monkeypatch):
        """No-op when storage is on local filesystem."""
        storage_dir = tmp_path / '.local' / 'share' / 'containers' / 'storage'
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        monkeypatch.setattr(podrun_mod, '_is_network_fs', lambda p: False)
        ns = {'root.nfs_remediate': 'init'}
        _nfs_remediate(self._ctx(ns))
        assert not storage_dir.exists()

    # -- Already symlinked ----------------------------------------------------

    def test_idempotent_correct_symlink(self, tmp_path, monkeypatch):
        """No-op when already symlinked to the expected target."""
        base = tmp_path / 'local-storage'
        user_store = base / UNAME
        user_store.mkdir(parents=True)
        storage_dir = tmp_path / '.local' / 'share' / 'containers' / 'storage'
        storage_dir.parent.mkdir(parents=True)
        storage_dir.symlink_to(user_store)
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        monkeypatch.setattr(podrun_mod, '_is_network_fs', lambda p: True)
        ns = {'root.nfs_remediate': 'init', 'root.nfs_remediate_path': str(base)}
        _nfs_remediate(self._ctx(ns))
        assert storage_dir.is_symlink()

    def test_different_symlink_warns(self, tmp_path, monkeypatch, capsys):
        """Warning when symlinked to a different target."""
        other = tmp_path / 'other-target'
        other.mkdir()
        storage_dir = tmp_path / '.local' / 'share' / 'containers' / 'storage'
        storage_dir.parent.mkdir(parents=True)
        storage_dir.symlink_to(other)
        base = tmp_path / 'local-storage'
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        ns = {'root.nfs_remediate': 'init', 'root.nfs_remediate_path': str(base)}
        _nfs_remediate(self._ctx(ns))
        assert 'Warning' in capsys.readouterr().err

    # -- Easy case: storage absent + NFS → create symlink ---------------------

    def test_creates_symlink_when_absent(self, tmp_path, monkeypatch, capsys):
        """Creates symlink when storage dir doesn't exist and FS is NFS."""
        base = tmp_path / 'local-storage'
        base.mkdir()
        storage_dir = tmp_path / '.local' / 'share' / 'containers' / 'storage'
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        monkeypatch.setattr(podrun_mod, '_is_network_fs', lambda p: True)
        ns = {'root.nfs_remediate': 'init', 'root.nfs_remediate_path': str(base)}
        _nfs_remediate(self._ctx(ns))
        assert storage_dir.is_symlink()
        assert storage_dir.resolve() == (base / UNAME).resolve()
        assert 'Created symlink' in capsys.readouterr().err

    # -- Mode: error (NFS detected → error, no action) -----------------------

    def test_error_mode_exits_on_nfs(self, tmp_path, monkeypatch, capsys):
        """error mode detects NFS and exits without taking action."""
        storage_dir = tmp_path / '.local' / 'share' / 'containers' / 'storage'
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        monkeypatch.setattr(podrun_mod, '_is_network_fs', lambda p: True)
        ns = {'root.nfs_remediate': 'error'}
        with pytest.raises(SystemExit):
            _nfs_remediate(self._ctx(ns))
        err = capsys.readouterr().err
        assert 'network filesystem' in err
        assert not storage_dir.exists()

    def test_error_mode_noop_on_local_fs(self, tmp_path, monkeypatch):
        """error mode does nothing when FS is local."""
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        monkeypatch.setattr(podrun_mod, '_is_network_fs', lambda p: False)
        ns = {'root.nfs_remediate': 'error'}
        _nfs_remediate(self._ctx(ns))

    # -- Vacant store (scaffolding only) ---------------------------------------

    def test_vacant_store_removed_silently(self, tmp_path, monkeypatch, capsys):
        """Vacant store (no *-images dir) is removed and symlinked."""
        base = tmp_path / 'local-storage'
        base.mkdir()
        storage_dir = tmp_path / '.local' / 'share' / 'containers' / 'storage'
        storage_dir.mkdir(parents=True)
        # Simulate podman scaffolding (no overlay-images dir)
        (storage_dir / 'overlay').mkdir()
        (storage_dir / 'storage.lock').write_text('')
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        monkeypatch.setattr(podrun_mod, '_is_network_fs', lambda p: True)
        ns = {'root.nfs_remediate': 'init', 'root.nfs_remediate_path': str(base)}
        _nfs_remediate(self._ctx(ns))
        assert storage_dir.is_symlink()
        assert 'Created symlink' in capsys.readouterr().err

    def test_vacant_store_removed_in_mv_mode(self, tmp_path, monkeypatch, capsys):
        """mv mode also removes vacant stores silently (nothing to move)."""
        base = tmp_path / 'local-storage'
        base.mkdir()
        storage_dir = tmp_path / '.local' / 'share' / 'containers' / 'storage'
        storage_dir.mkdir(parents=True)
        (storage_dir / 'storage.lock').write_text('')
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        monkeypatch.setattr(podrun_mod, '_is_network_fs', lambda p: True)
        ns = {'root.nfs_remediate': 'mv', 'root.nfs_remediate_path': str(base)}
        _nfs_remediate(self._ctx(ns))
        assert storage_dir.is_symlink()

    # -- Mode: init (non-vacant existing dir → error) -------------------------

    def test_init_mode_errors_on_non_vacant_dir(self, tmp_path, monkeypatch):
        """init mode errors when storage has real data."""
        base = tmp_path / 'local-storage'
        base.mkdir()
        storage_dir = tmp_path / '.local' / 'share' / 'containers' / 'storage'
        storage_dir.mkdir(parents=True)
        (storage_dir / 'overlay-images').mkdir()  # marks store as non-vacant
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        monkeypatch.setattr(podrun_mod, '_is_network_fs', lambda p: True)
        ns = {'root.nfs_remediate': 'init', 'root.nfs_remediate_path': str(base)}
        with pytest.raises(SystemExit):
            _nfs_remediate(self._ctx(ns))

    # -- Mode: mv (existing dir → move + symlink) ----------------------------

    def test_mv_mode_moves_contents(self, tmp_path, monkeypatch, capsys):
        """mv mode moves contents and creates symlink."""
        base = tmp_path / 'local-storage'
        base.mkdir()
        storage_dir = tmp_path / '.local' / 'share' / 'containers' / 'storage'
        storage_dir.mkdir(parents=True)
        (storage_dir / 'overlay-images').mkdir()
        (storage_dir / 'some-file.txt').write_text('data')
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        monkeypatch.setattr(podrun_mod, '_is_network_fs', lambda p: True)
        ns = {'root.nfs_remediate': 'mv', 'root.nfs_remediate_path': str(base)}
        _nfs_remediate(self._ctx(ns))
        user_store = base / UNAME
        assert storage_dir.is_symlink()
        assert (user_store / 'overlay-images').is_dir()
        assert (user_store / 'some-file.txt').read_text() == 'data'
        assert 'Moving' in capsys.readouterr().err

    def test_mv_mode_skips_existing_items(self, tmp_path, monkeypatch, capsys):
        """mv mode skips items that already exist at destination."""
        base = tmp_path / 'local-storage'
        user_store = base / UNAME
        user_store.mkdir(parents=True)
        (user_store / 'existing').write_text('keep')
        storage_dir = tmp_path / '.local' / 'share' / 'containers' / 'storage'
        storage_dir.mkdir(parents=True)
        (storage_dir / 'overlay-images').mkdir()
        (storage_dir / 'existing').write_text('discard')
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        monkeypatch.setattr(podrun_mod, '_is_network_fs', lambda p: True)
        ns = {'root.nfs_remediate': 'mv', 'root.nfs_remediate_path': str(base)}
        _nfs_remediate(self._ctx(ns))
        assert (user_store / 'existing').read_text() == 'keep'
        assert 'skip (exists)' in capsys.readouterr().err

    # -- Mode: rm (existing dir → remove + symlink) --------------------------

    def test_rm_mode_removes_dir(self, tmp_path, monkeypatch, capsys):
        """rm mode removes storage dir and creates symlink."""
        base = tmp_path / 'local-storage'
        base.mkdir()
        storage_dir = tmp_path / '.local' / 'share' / 'containers' / 'storage'
        storage_dir.mkdir(parents=True)
        (storage_dir / 'overlay-images').mkdir()
        (storage_dir / 'data').write_text('bye')
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        monkeypatch.setattr(podrun_mod, '_is_network_fs', lambda p: True)
        ns = {'root.nfs_remediate': 'rm', 'root.nfs_remediate_path': str(base)}
        _nfs_remediate(self._ctx(ns))
        assert storage_dir.is_symlink()
        assert 'Removing' in capsys.readouterr().err

    # -- Mode: prompt (interactive) -------------------------------------------

    def test_prompt_non_interactive_errors(self, tmp_path, monkeypatch):
        """prompt mode errors in non-interactive session."""
        base = tmp_path / 'local-storage'
        base.mkdir()
        storage_dir = tmp_path / '.local' / 'share' / 'containers' / 'storage'
        storage_dir.mkdir(parents=True)
        (storage_dir / 'overlay-images').mkdir()
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        monkeypatch.setattr(podrun_mod, '_is_network_fs', lambda p: True)
        monkeypatch.setattr(podrun_mod.sys.stdin, 'isatty', lambda: False)
        ns = {'root.nfs_remediate': 'prompt', 'root.nfs_remediate_path': str(base)}
        with pytest.raises(SystemExit):
            _nfs_remediate(self._ctx(ns))

    def test_prompt_move_accepted(self, tmp_path, monkeypatch, capsys):
        """prompt mode: user accepts move."""
        base = tmp_path / 'local-storage'
        base.mkdir()
        storage_dir = tmp_path / '.local' / 'share' / 'containers' / 'storage'
        storage_dir.mkdir(parents=True)
        (storage_dir / 'overlay-images').mkdir()
        (storage_dir / 'item').write_text('x')
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        monkeypatch.setattr(podrun_mod, '_is_network_fs', lambda p: True)
        monkeypatch.setattr(podrun_mod.sys.stdin, 'isatty', lambda: True)
        monkeypatch.setattr(
            podrun_mod, 'yes_no_prompt', lambda msg, default, interactive: 'Move' in msg
        )
        ns = {'root.nfs_remediate': 'prompt', 'root.nfs_remediate_path': str(base)}
        _nfs_remediate(self._ctx(ns))
        assert storage_dir.is_symlink()
        assert (base / UNAME / 'item').read_text() == 'x'

    def test_prompt_remove_accepted(self, tmp_path, monkeypatch, capsys):
        """prompt mode: user declines move, accepts remove."""
        base = tmp_path / 'local-storage'
        base.mkdir()
        storage_dir = tmp_path / '.local' / 'share' / 'containers' / 'storage'
        storage_dir.mkdir(parents=True)
        (storage_dir / 'overlay-images').mkdir()
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        monkeypatch.setattr(podrun_mod, '_is_network_fs', lambda p: True)
        monkeypatch.setattr(podrun_mod.sys.stdin, 'isatty', lambda: True)
        # First prompt (Move?) → No, second prompt (Remove?) → Yes
        responses = iter([False, True])
        monkeypatch.setattr(
            podrun_mod, 'yes_no_prompt', lambda msg, default, interactive: next(responses)
        )
        ns = {'root.nfs_remediate': 'prompt', 'root.nfs_remediate_path': str(base)}
        _nfs_remediate(self._ctx(ns))
        assert storage_dir.is_symlink()

    def test_prompt_cancelled(self, tmp_path, monkeypatch):
        """prompt mode: user declines both → exit 0."""
        base = tmp_path / 'local-storage'
        base.mkdir()
        storage_dir = tmp_path / '.local' / 'share' / 'containers' / 'storage'
        storage_dir.mkdir(parents=True)
        (storage_dir / 'overlay-images').mkdir()
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        monkeypatch.setattr(podrun_mod, '_is_network_fs', lambda p: True)
        monkeypatch.setattr(podrun_mod.sys.stdin, 'isatty', lambda: True)
        monkeypatch.setattr(podrun_mod, 'yes_no_prompt', lambda msg, default, interactive: False)
        ns = {'root.nfs_remediate': 'prompt', 'root.nfs_remediate_path': str(base)}
        with pytest.raises(SystemExit) as exc_info:
            _nfs_remediate(self._ctx(ns))
        assert exc_info.value.code == 0

    # -- Custom path ----------------------------------------------------------

    def test_custom_path(self, tmp_path, monkeypatch, capsys):
        """--nfs-remediate-path overrides the default base."""
        custom_base = tmp_path / 'custom-store'
        custom_base.mkdir()
        storage_dir = tmp_path / '.local' / 'share' / 'containers' / 'storage'
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        monkeypatch.setattr(podrun_mod, '_is_network_fs', lambda p: True)
        ns = {'root.nfs_remediate': 'init', 'root.nfs_remediate_path': str(custom_base)}
        _nfs_remediate(self._ctx(ns))
        assert storage_dir.is_symlink()
        assert storage_dir.resolve() == (custom_base / UNAME).resolve()

    # -- Default path constant ------------------------------------------------

    def test_default_base_constant(self):
        assert _NFS_REMEDIATE_DEFAULT_BASE == '/opt/podman-local-storage'

    # -- Sudo failure ---------------------------------------------------------

    def test_sudo_failure_helpful_error(self, tmp_path, monkeypatch, capsys):
        """Helpful error when sudo mkdir fails."""
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        monkeypatch.setattr(podrun_mod, '_is_network_fs', lambda p: True)
        # Use a base path that doesn't exist and mock subprocess.run to fail
        fake_base = '/nonexistent/nfs-store'
        ns = {'root.nfs_remediate': 'init', 'root.nfs_remediate_path': fake_base}

        orig_run = subprocess.run

        def mock_run(cmd, **kwargs):
            if cmd[:2] == ['sudo', 'mkdir']:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=1,
                    stdout='',
                    stderr='sudo: permission denied',
                )
            return orig_run(cmd, **kwargs)

        monkeypatch.setattr(podrun_mod.subprocess, 'run', mock_run)
        with pytest.raises(SystemExit):
            _nfs_remediate(self._ctx(ns))
        err = capsys.readouterr().err
        assert '--nfs-remediate-path' in err

    # -- _is_vacant_store -----------------------------------------------------

    def test_is_vacant_store_empty_dir(self, tmp_path):
        d = tmp_path / 'storage'
        d.mkdir()
        assert _is_vacant_store(d) is True

    def test_is_vacant_store_scaffolding(self, tmp_path):
        """Scaffolding from 'podman ps' (no *-images dir) is vacant."""
        d = tmp_path / 'storage'
        d.mkdir()
        (d / 'overlay').mkdir()
        (d / 'overlay' / 'l').mkdir()
        (d / 'storage.lock').write_text('')
        (d / 'libpod').mkdir()
        (d / 'libpod' / 'bolt_state.db').write_bytes(b'\x00' * 100)
        assert _is_vacant_store(d) is True

    def test_is_vacant_store_with_images(self, tmp_path):
        """Store with overlay-images/ is non-vacant."""
        d = tmp_path / 'storage'
        d.mkdir()
        (d / 'overlay-images').mkdir()
        assert _is_vacant_store(d) is False

    def test_is_vacant_store_vfs_images(self, tmp_path):
        """Store with vfs-images/ is non-vacant (driver-agnostic)."""
        d = tmp_path / 'storage'
        d.mkdir()
        (d / 'vfs-images').mkdir()
        assert _is_vacant_store(d) is False

    # -- _is_network_fs -------------------------------------------------------

    def test_is_network_fs_local(self, tmp_path, monkeypatch):
        """Local filesystem returns False."""
        assert _is_network_fs(str(tmp_path)) is False

    def test_is_network_fs_nonexistent_walks_up(self, tmp_path):
        """Non-existent path walks up to existing ancestor."""
        deep = tmp_path / 'a' / 'b' / 'c'
        result = _is_network_fs(str(deep))
        assert result is False  # tmp_path is local

    def test_is_network_fs_stat_returns_nfs(self, tmp_path, monkeypatch):
        """stat reporting NFS returns True."""

        def mock_run(cmd, **kwargs):
            if cmd[0] == 'stat':
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout='nfs\n', stderr=''
                )
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout='', stderr='')

        monkeypatch.setattr(podrun_mod.subprocess, 'run', mock_run)
        assert _is_network_fs(str(tmp_path)) is True

    def test_is_network_fs_stat_fails_df_fallback_nfs(self, tmp_path, monkeypatch):
        """df -T fallback detects NFS when stat fails."""

        def mock_run(cmd, **kwargs):
            if cmd[0] == 'stat':
                return subprocess.CompletedProcess(args=cmd, returncode=1, stdout='', stderr='err')
            if cmd[0] == 'df':
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout='Filesystem     Type  Size  Used Avail Use% Mounted on\nserver:/vol  nfs4   50G   20G   30G  40% /home\n',
                    stderr='',
                )
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout='', stderr='')

        monkeypatch.setattr(podrun_mod.subprocess, 'run', mock_run)
        assert _is_network_fs(str(tmp_path)) is True

    def test_is_network_fs_stat_fails_df_local(self, tmp_path, monkeypatch):
        """df -T fallback returns False for local fs."""

        def mock_run(cmd, **kwargs):
            if cmd[0] == 'stat':
                return subprocess.CompletedProcess(args=cmd, returncode=1, stdout='', stderr='err')
            if cmd[0] == 'df':
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout='Filesystem     Type  Size\n/dev/sda1      ext4  100G\n',
                    stderr='',
                )
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout='', stderr='')

        monkeypatch.setattr(podrun_mod.subprocess, 'run', mock_run)
        assert _is_network_fs(str(tmp_path)) is False

    def test_is_network_fs_both_commands_missing(self, tmp_path, monkeypatch):
        """Returns False when both stat and df are missing."""

        def mock_run(cmd, **kwargs):
            raise FileNotFoundError(cmd[0])

        monkeypatch.setattr(podrun_mod.subprocess, 'run', mock_run)
        assert _is_network_fs(str(tmp_path)) is False

    def test_is_network_fs_root_walk(self):
        """Walks to root when path is purely non-existent."""
        result = _is_network_fs('/nonexistent/deep/path')
        assert isinstance(result, bool)

    def test_sudo_chmod_runs_on_success(self, tmp_path, monkeypatch, capsys):
        """sudo chmod 1777 runs after successful sudo mkdir."""
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        monkeypatch.setattr(podrun_mod, '_is_network_fs', lambda p: True)
        fake_base = tmp_path / 'needs-sudo'
        ns = {'root.nfs_remediate': 'init', 'root.nfs_remediate_path': str(fake_base)}
        sudo_calls = []
        orig_run = subprocess.run

        def mock_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[0] == 'sudo':
                sudo_calls.append(cmd)
                if cmd[1] == 'mkdir':
                    fake_base.mkdir(parents=True, exist_ok=True)
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout='', stderr='')
            return orig_run(cmd, **kwargs)

        monkeypatch.setattr(podrun_mod.subprocess, 'run', mock_run)
        _nfs_remediate(self._ctx(ns))
        assert any(c[1] == 'chmod' for c in sudo_calls)

    def test_prompt_move_skip_existing(self, tmp_path, monkeypatch, capsys):
        """prompt mode: move skips items that already exist at destination."""
        base = tmp_path / 'local-storage'
        user_store = base / UNAME
        user_store.mkdir(parents=True)
        (user_store / 'existing').write_text('keep')
        storage_dir = tmp_path / '.local' / 'share' / 'containers' / 'storage'
        storage_dir.mkdir(parents=True)
        (storage_dir / 'overlay-images').mkdir()
        (storage_dir / 'existing').write_text('discard')
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        monkeypatch.setattr(podrun_mod, '_is_network_fs', lambda p: True)
        monkeypatch.setattr(podrun_mod.sys.stdin, 'isatty', lambda: True)
        monkeypatch.setattr(
            podrun_mod, 'yes_no_prompt', lambda msg, default, interactive: 'Move' in msg
        )
        ns = {'root.nfs_remediate': 'prompt', 'root.nfs_remediate_path': str(base)}
        _nfs_remediate(self._ctx(ns))
        assert (user_store / 'existing').read_text() == 'keep'
        assert 'skip (exists)' in capsys.readouterr().err


# ---------------------------------------------------------------------------
# initializeCommand — host-side lifecycle via _handle_run
# ---------------------------------------------------------------------------


class TestInitializeCommandInHandleRun:
    """Verify initializeCommand is invoked (or skipped) by _handle_run via --print-cmd."""

    @pytest.fixture(autouse=True)
    def _mock_run_os_cmd(self, monkeypatch):
        monkeypatch.setattr(
            podrun_mod,
            'run_os_cmd',
            lambda cmd, env=None: subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout='', stderr=''
            ),
        )

    def test_skipped_with_print_cmd(self, monkeypatch, capsys, tmp_path):
        """initializeCommand is NOT executed when --print-cmd is set."""
        dc_dir = tmp_path / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        import json

        dc_file.write_text(
            json.dumps({'image': 'alpine', 'initializeCommand': 'echo SHOULD_NOT_RUN'})
        )
        monkeypatch.setattr(podrun_mod, 'find_devcontainer_json', lambda start_dir=None: dc_file)
        # Capture that _run_initialize_command is not called
        calls = []
        orig = _run_initialize_command
        monkeypatch.setattr(
            podrun_mod,
            '_run_initialize_command',
            lambda cmd: calls.append(cmd) or orig(cmd),
        )
        with pytest.raises(SystemExit) as exc:
            main(['--print-cmd', 'run', 'alpine'])
        assert exc.value.code == 0
        assert len(calls) == 0

    def test_skipped_when_dc_cli_drives(self, monkeypatch, capsys, tmp_path):
        """initializeCommand is NOT executed when devcontainer CLI is driving."""
        dc_dir = tmp_path / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        import json

        dc_file.write_text(
            json.dumps({'image': 'alpine', 'initializeCommand': 'echo SHOULD_NOT_RUN'})
        )
        monkeypatch.setattr(podrun_mod, 'find_devcontainer_json', lambda start_dir=None: dc_file)
        calls = []
        monkeypatch.setattr(podrun_mod, '_run_initialize_command', lambda cmd: calls.append(cmd))
        with pytest.raises(SystemExit) as exc:
            main(
                [
                    '--print-cmd',
                    'run',
                    '-l',
                    f'devcontainer.config_file={dc_file}',
                    'alpine',
                ]
            )
        assert exc.value.code == 0
        assert len(calls) == 0
