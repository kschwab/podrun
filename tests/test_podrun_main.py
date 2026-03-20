"""Tests for Phase 2.5 — main orchestration + execution."""

import shlex
import shutil
import subprocess

import pytest

import podrun.podrun as podrun_mod
from podrun.podrun import (
    ENV_PODRUN_PODMAN_PATH,
    ENV_PODRUN_PODMAN_REMOTE,
    UNAME,
    PodrunContext,
    _default_podman_path,
    _discover_podrunrc,
    _filter_global_args,
    _is_remote,
    _resolve_overlay_mounts,
    _warn_missing_subids,
    main,
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

    def _run(self, argv, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(['--print-cmd'] + argv)
        assert exc_info.value.code == 0
        return shlex.split(capsys.readouterr().out)

    def test_rc_flags_appear_in_command(self, monkeypatch, capsys):
        """~/.podrunrc output is parsed and appears in the podman command."""
        monkeypatch.setattr(podrun_mod, '_discover_podrunrc', lambda: '/fake/.podrunrc')

        # Mock run_os_cmd to return --session when called with the rc script,
        # and empty output for other calls (stale cleanup, etc.)
        def mock_cmd(cmd, env=None):
            if '/fake/.podrunrc' in cmd:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout='--session\n',
                    stderr='',
                )
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='',
                stderr='',
            )

        monkeypatch.setattr(podrun_mod, 'run_os_cmd', mock_cmd)
        cmd = self._run(['run', 'alpine'], capsys)
        # --session implies -it and --userns=keep-id
        assert '-it' in cmd
        assert '--userns=keep-id' in cmd

    def test_rc_passthrough_flags_in_command(self, monkeypatch, capsys):
        """Passthrough flags from rc appear in the podman command."""
        monkeypatch.setattr(podrun_mod, '_discover_podrunrc', lambda: '/fake/.podrunrc')

        def mock_cmd(cmd, env=None):
            if '/fake/.podrunrc' in cmd:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout='-e RC_VAR=1\n',
                    stderr='',
                )
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='',
                stderr='',
            )

        monkeypatch.setattr(podrun_mod, 'run_os_cmd', mock_cmd)
        cmd = self._run(['run', 'alpine'], capsys)
        assert '-e' in cmd
        assert 'RC_VAR=1' in cmd

    def test_cli_overrides_rc(self, monkeypatch, capsys):
        """CLI flags take precedence over rc flags."""
        monkeypatch.setattr(podrun_mod, '_discover_podrunrc', lambda: '/fake/.podrunrc')

        def mock_cmd(cmd, env=None):
            if '/fake/.podrunrc' in cmd:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout='--shell /bin/bash\n',
                    stderr='',
                )
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='',
                stderr='',
            )

        monkeypatch.setattr(podrun_mod, 'run_os_cmd', mock_cmd)
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
        monkeypatch.setattr(podrun_mod, '_discover_podrunrc', lambda: '/fake/.podrunrc')

        def mock_cmd(cmd, env=None):
            if '/fake/.podrunrc' in cmd:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout='--name rc-name\n',
                    stderr='',
                )
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='',
                stderr='',
            )

        monkeypatch.setattr(podrun_mod, 'run_os_cmd', mock_cmd)
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
        monkeypatch.setattr(podrun_mod, '_discover_podrunrc', lambda: '/fake/.podrunrc')

        def mock_cmd(cmd, env=None):
            if '/fake/.podrunrc' in cmd:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=f'--export /rc_src:{rc_dst}\n',
                    stderr='',
                )
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='',
                stderr='',
            )

        monkeypatch.setattr(podrun_mod, 'run_os_cmd', mock_cmd)
        cmd = self._run(
            ['run', '--user-overlay', '--export', f'/cli_src:{cli_dst}', 'alpine'],
            capsys,
        )
        # All three export volumes should be present
        joined = ' '.join(cmd)
        assert str(rc_dst) in joined
        assert str(dc_dst) in joined
        assert str(cli_dst) in joined

    def test_rc_passthrough_lowest_priority(self, monkeypatch, capsys):
        """rc passthrough args come before dc/script/cli passthrough."""
        monkeypatch.setattr(podrun_mod, '_discover_podrunrc', lambda: '/fake/.podrunrc')

        def mock_cmd(cmd, env=None):
            if '/fake/.podrunrc' in cmd:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout='-e FROM_RC=1\n',
                    stderr='',
                )
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='',
                stderr='',
            )

        monkeypatch.setattr(podrun_mod, 'run_os_cmd', mock_cmd)
        cmd = self._run(['run', '-e', 'FROM_CLI=1', 'alpine'], capsys)
        # Both env vars should be present
        joined = ' '.join(cmd)
        assert 'FROM_RC=1' in joined
        assert 'FROM_CLI=1' in joined
        # RC passthrough should appear before CLI passthrough
        rc_idx = next(i for i, a in enumerate(cmd) if a == 'FROM_RC=1')
        cli_idx = next(i for i, a in enumerate(cmd) if a == 'FROM_CLI=1')
        assert rc_idx < cli_idx
