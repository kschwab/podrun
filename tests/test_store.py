"""Tests for the ``podrun store`` subcommand."""

import json
import os
import pathlib
import shutil
import subprocess

import pytest

import podrun.podrun as podrun_mod
from podrun.podrun import (
    PODMAN_SUBCOMMANDS,
    _PODRUN_SUBCOMMANDS,
    _detect_subcommand,
    _find_project_context,
    _has_store_conflict,
    _resolve_store_flags,
    _runroot_path,
    _warn_missing_subids,
    main,
)


class TestRunrootPath:
    def test_deterministic(self):
        """Same input produces same output."""
        assert _runroot_path('/a/b/c') == _runroot_path('/a/b/c')

    def test_unique_per_input(self):
        """Different inputs produce different hashes."""
        assert _runroot_path('/a/b/c') != _runroot_path('/x/y/z')

    def test_under_stores_dir(self):
        """Result is under _PODRUN_STORES_DIR."""
        result = _runroot_path('/some/graphroot')
        assert result.startswith(podrun_mod._PODRUN_STORES_DIR)

    def test_path_short(self):
        """Result is well under the 108-byte sun_path limit."""
        result = _runroot_path('/some/very/long/graphroot/path/here')
        # Path = stores_dir + '/' + 12-char hash
        assert len(result) < 108


class TestDetectSubcommandStore:
    def test_store_detected(self):
        assert _detect_subcommand(['store', 'init']) == ('store', 0)

    def test_store_with_global_flags(self):
        assert _detect_subcommand(['--root=/x', 'store', 'init']) == ('store', 1)


class TestStoreInit:
    def test_creates_structure(self, tmp_path, monkeypatch):
        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        assert (store_dir / 'graphroot').is_dir()

    def test_runroot_is_symlink(self, tmp_path, monkeypatch):
        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        runroot = store_dir / 'runroot'
        assert runroot.is_symlink()
        target = os.readlink(str(runroot))
        assert target.startswith(podrun_mod._PODRUN_STORES_DIR)

    def test_with_registry(self, tmp_path, monkeypatch):
        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir), '--registry', 'mirror.example.com'])
        reg = store_dir / 'registries.conf'
        assert reg.exists()
        content = reg.read_text()
        assert 'mirror.example.com' in content

    def test_idempotent(self, tmp_path, monkeypatch):
        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        # Run again — should not error
        main(['store', 'init', '--store-dir', str(store_dir)])
        assert (store_dir / 'graphroot').is_dir()

    def test_no_action_prints_help(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(['store'])
        assert exc_info.value.code == 1

    def test_podman_not_found(self, tmp_path, monkeypatch):
        store_dir = tmp_path / 'test-store'
        monkeypatch.setattr(podrun_mod.shutil, 'which', lambda x: None)
        with pytest.raises(SystemExit) as exc_info:
            main(['store', 'init', '--store-dir', str(store_dir)])
        assert exc_info.value.code == 1

    def test_prints_summary(self, tmp_path, monkeypatch, capsys):
        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        out = capsys.readouterr().out
        assert 'initialized' in out.lower() or 'Podrun store' in out
        assert 'graphroot' in out


class TestStoreDestroy:
    def test_removes_store_dir(self, tmp_path, monkeypatch):
        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        assert store_dir.exists()
        main(['store', 'destroy', '--store-dir', str(store_dir)])
        assert not store_dir.exists()

    def test_removes_runroot_target(self, tmp_path, monkeypatch):
        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        runroot_target = os.readlink(str(store_dir / 'runroot'))
        assert pathlib.Path(runroot_target).exists()
        main(['store', 'destroy', '--store-dir', str(store_dir)])
        assert not pathlib.Path(runroot_target).exists()

    def test_prints_what_removed(self, tmp_path, monkeypatch, capsys):
        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        capsys.readouterr()  # discard init output
        main(['store', 'destroy', '--store-dir', str(store_dir)])
        out = capsys.readouterr().out
        assert 'Removed' in out

    def test_nonexistent_errors(self, tmp_path, capsys):
        store_dir = tmp_path / 'nonexistent'
        with pytest.raises(SystemExit) as exc_info:
            main(['store', 'destroy', '--store-dir', str(store_dir)])
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert 'does not exist' in err

    def test_cleans_parent_when_empty(self, tmp_path, monkeypatch, capsys):
        """When the podrun-stores parent dir is empty after destroy, it is removed."""
        # Use an isolated parent dir so we don't affect the real /tmp/podrun-stores/
        fake_parent = tmp_path / 'podrun-stores'
        fake_parent.mkdir()
        monkeypatch.setattr(podrun_mod, '_PODRUN_STORES_DIR', str(fake_parent))

        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        capsys.readouterr()  # discard init output
        main(['store', 'destroy', '--store-dir', str(store_dir)])
        out = capsys.readouterr().out
        assert 'Removed' in out
        assert not fake_parent.exists(), 'Empty parent should be removed'
        assert str(fake_parent) in out

    def test_destroy_survives_podman_reset_failure(self, tmp_path, monkeypatch, capsys):
        """Destroy succeeds even when podman system reset fails."""
        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        capsys.readouterr()
        # Make subprocess.run raise OSError for the reset call
        real_run = subprocess.run

        def _failing_run(cmd, **kwargs):
            if 'system' in cmd and 'reset' in cmd:
                raise OSError('fake podman failure')
            return real_run(cmd, **kwargs)

        monkeypatch.setattr(podrun_mod.subprocess, 'run', _failing_run)
        main(['store', 'destroy', '--store-dir', str(store_dir)])
        assert not store_dir.exists()

    def test_broken_symlink(self, tmp_path, monkeypatch, capsys):
        """Destroy succeeds when runroot symlink target was already deleted."""
        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        # Delete the runroot target so the symlink is broken
        runroot_target = os.readlink(str(store_dir / 'runroot'))
        if os.path.exists(runroot_target):
            os.rmdir(runroot_target)
        capsys.readouterr()
        main(['store', 'destroy', '--store-dir', str(store_dir)])
        assert not store_dir.exists()
        out = capsys.readouterr().out
        assert 'Removed' in out
        # Should NOT print that it removed the runroot target
        assert runroot_target not in out

    def test_missing_symlink(self, tmp_path, monkeypatch, capsys):
        """Destroy succeeds when runroot symlink was manually removed."""
        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        # Remove the symlink itself
        (store_dir / 'runroot').unlink()
        capsys.readouterr()
        main(['store', 'destroy', '--store-dir', str(store_dir)])
        assert not store_dir.exists()
        out = capsys.readouterr().out
        assert 'Removed' in out

    def test_partial_store(self, tmp_path, capsys):
        """Destroy succeeds on a store directory missing graphroot."""
        store_dir = tmp_path / 'test-store'
        store_dir.mkdir()
        # No graphroot, no runroot symlink — just a bare directory
        (store_dir / 'some-file').write_text('leftover')
        main(['store', 'destroy', '--store-dir', str(store_dir)])
        assert not store_dir.exists()
        out = capsys.readouterr().out
        assert 'Removed' in out

    def test_non_dir_graphroot_glob_skipped(self, tmp_path, monkeypatch, capsys):
        """Destroy skips non-directory entries matching graphroot* glob."""
        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        # Create a file (not directory) matching the graphroot* pattern
        (store_dir / 'graphroot-stale-file').write_text('not a dir')
        capsys.readouterr()
        main(['store', 'destroy', '--store-dir', str(store_dir)])
        assert not store_dir.exists()

    def test_permission_error_fallback(self, tmp_path, monkeypatch, capsys):
        """Destroy falls back to podman unshare rm when rmtree hits PermissionError."""
        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        capsys.readouterr()

        real_rmtree = shutil.rmtree
        called_unshare = []

        def _perm_error_rmtree(path, **kwargs):
            if str(path) == str(store_dir):
                # First call raises PermissionError, then the fallback
                # uses podman unshare rm -rf which we simulate by calling
                # the real rmtree.
                raise PermissionError('UID-mapped overlay')
            real_rmtree(path, **kwargs)

        real_run = subprocess.run

        def _capture_unshare_run(cmd, **kwargs):
            if isinstance(cmd, list) and 'unshare' in cmd and 'rm' in cmd:
                called_unshare.append(cmd)
                # Actually remove the dir so the exists() check passes
                real_rmtree(str(store_dir))
                return subprocess.CompletedProcess(cmd, 0)
            return real_run(cmd, **kwargs)

        monkeypatch.setattr(podrun_mod.shutil, 'rmtree', _perm_error_rmtree)
        monkeypatch.setattr(podrun_mod.subprocess, 'run', _capture_unshare_run)

        main(['store', 'destroy', '--store-dir', str(store_dir)])
        assert not store_dir.exists()
        assert len(called_unshare) == 1
        assert 'unshare' in called_unshare[0]

    def test_permission_error_fallback_fails(self, tmp_path, monkeypatch, capsys):
        """Destroy exits with error when podman unshare rm also fails."""
        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        capsys.readouterr()

        def _perm_error_rmtree(path, **kwargs):
            if str(path) == str(store_dir):
                raise PermissionError('UID-mapped overlay')

        real_run = subprocess.run

        def _noop_unshare_run(cmd, **kwargs):
            if isinstance(cmd, list) and 'unshare' in cmd and 'rm' in cmd:
                # Don't actually remove — simulate failure
                return subprocess.CompletedProcess(cmd, 0)
            return real_run(cmd, **kwargs)

        monkeypatch.setattr(podrun_mod.shutil, 'rmtree', _perm_error_rmtree)
        monkeypatch.setattr(podrun_mod.subprocess, 'run', _noop_unshare_run)

        with pytest.raises(SystemExit) as exc_info:
            main(['store', 'destroy', '--store-dir', str(store_dir)])
        assert exc_info.value.code == 1


class TestStoreInfo:
    def test_existing_store(self, tmp_path, monkeypatch, capsys):
        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        capsys.readouterr()  # discard init output
        main(['store', 'info', '--store-dir', str(store_dir)])
        out = capsys.readouterr().out
        assert 'Podrun store' in out
        assert 'graphroot' in out
        assert 'runroot' in out

    def test_nonexistent_store(self, tmp_path, capsys):
        store_dir = tmp_path / 'nonexistent'
        with pytest.raises(SystemExit) as exc_info:
            main(['store', 'info', '--store-dir', str(store_dir)])
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert 'No store found' in err
        assert 'podrun store init' in err

    def test_shows_registry_when_present(self, tmp_path, monkeypatch, capsys):
        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir), '--registry', 'mirror.example.com'])
        capsys.readouterr()
        main(['store', 'info', '--store-dir', str(store_dir)])
        out = capsys.readouterr().out
        assert 'registries' in out

    def test_missing_runroot_noted(self, tmp_path, monkeypatch, capsys):
        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        # Delete the runroot target (simulates post-reboot)
        runroot_target = os.readlink(str(store_dir / 'runroot'))
        if os.path.isdir(runroot_target):
            os.rmdir(runroot_target)
        capsys.readouterr()
        main(['store', 'info', '--store-dir', str(store_dir)])
        out = capsys.readouterr().out
        assert 'missing' in out

    def test_init_output_matches_info(self, tmp_path, monkeypatch, capsys):
        """store init and store info should show the same store summary."""
        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        init_out = capsys.readouterr().out
        main(['store', 'info', '--store-dir', str(store_dir)])
        info_out = capsys.readouterr().out
        assert info_out == init_out


class TestSubcommandSets:
    def test_podrun_subcommands_disjoint_from_podman(self):
        """Podrun-specific subcommands must not overlap with podman subcommands."""
        overlap = PODMAN_SUBCOMMANDS & _PODRUN_SUBCOMMANDS
        assert overlap == set(), f'Overlap: {overlap}'


class TestWarnMissingSubids:
    """Tests for _warn_missing_subids()."""

    def test_no_warning_when_user_found(self, monkeypatch, capsys, tmp_path):
        """No warning when user appears in both subuid and subgid."""
        subuid = tmp_path / 'subuid'
        subgid = tmp_path / 'subgid'
        subuid.write_text('testuser:100000:65536\n')
        subgid.write_text('testuser:100000:65536\n')
        monkeypatch.setattr('getpass.getuser', lambda: 'testuser')
        real_open = open

        def _mock_open(path, *a, **kw):
            if path == '/etc/subuid':
                return real_open(str(subuid), *a, **kw)
            if path == '/etc/subgid':
                return real_open(str(subgid), *a, **kw)
            return real_open(path, *a, **kw)

        monkeypatch.setattr('builtins.open', _mock_open)
        _warn_missing_subids()
        out = capsys.readouterr().out
        assert 'Note:' not in out

    def test_warning_when_user_missing(self, monkeypatch, capsys, tmp_path):
        """Warning printed when user is not in subuid/subgid."""
        subuid = tmp_path / 'subuid'
        subgid = tmp_path / 'subgid'
        subuid.write_text('otheruser:100000:65536\n')
        subgid.write_text('otheruser:100000:65536\n')
        monkeypatch.setattr('getpass.getuser', lambda: 'testuser')
        real_open = open

        def _mock_open(path, *a, **kw):
            if path == '/etc/subuid':
                return real_open(str(subuid), *a, **kw)
            if path == '/etc/subgid':
                return real_open(str(subgid), *a, **kw)
            return real_open(path, *a, **kw)

        monkeypatch.setattr('builtins.open', _mock_open)
        _warn_missing_subids()
        out = capsys.readouterr().out
        assert 'Note: testuser not found in /etc/subuid or /etc/subgid' in out
        assert '--userns=keep-id' in out
        assert (
            'sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 testuser' in out
        )

    def test_warning_when_files_missing(self, monkeypatch, capsys):
        """Warning printed when subuid/subgid files don't exist."""
        monkeypatch.setattr('getpass.getuser', lambda: 'testuser')
        real_open = open

        def _mock_open(path, *a, **kw):
            if path in ('/etc/subuid', '/etc/subgid'):
                raise FileNotFoundError(path)
            return real_open(path, *a, **kw)

        monkeypatch.setattr('builtins.open', _mock_open)
        _warn_missing_subids()
        out = capsys.readouterr().out
        assert 'Note: testuser not found in /etc/subuid or /etc/subgid' in out

    def test_exception_suppressed(self, monkeypatch, capsys):
        """Unexpected exceptions are silently suppressed."""
        monkeypatch.setattr('getpass.getuser', lambda: (_ for _ in ()).throw(RuntimeError))
        _warn_missing_subids()
        out = capsys.readouterr().out
        assert out == ''


class TestResolveStoreFlags:
    """Tests for _resolve_store_flags() and --store integration in main()."""

    def test_resolves_to_root_runroot_driver(self, tmp_path, monkeypatch):
        """--store resolves to --root/--runroot/--storage-driver in output."""
        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        flags, env = _resolve_store_flags(str(store_dir))
        assert flags[0] == '--root'
        assert flags[1] == str(store_dir / 'graphroot')
        assert flags[2] == '--runroot'
        assert flags[4] == '--storage-driver'
        assert flags[5] == 'overlay'

    def test_nonexistent_store_errors_without_auto_init(self, tmp_path, capsys):
        """--store with nonexistent dir errors without --auto-init-store."""
        store_dir = tmp_path / 'no-such-store'
        with pytest.raises(SystemExit) as exc_info:
            _resolve_store_flags(str(store_dir))
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert 'does not contain a graphroot' in err
        assert '--auto-init-store' in err

    def test_auto_init_creates_store(self, tmp_path, monkeypatch):
        """--auto-init-store creates store on first use."""
        store_dir = tmp_path / 'auto-store'
        flags, env = _resolve_store_flags(str(store_dir), auto_init=True)
        assert (store_dir / 'graphroot').is_dir()
        assert flags[0] == '--root'
        assert flags[1] == str(store_dir / 'graphroot')

    def test_auto_init_idempotent(self, tmp_path, monkeypatch):
        """--auto-init-store on existing store works without error."""
        store_dir = tmp_path / 'idem-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        flags1, _ = _resolve_store_flags(str(store_dir), auto_init=True)
        flags2, _ = _resolve_store_flags(str(store_dir), auto_init=True)
        assert flags1 == flags2

    def test_sets_registries_conf_env(self, tmp_path, monkeypatch):
        """--store sets CONTAINERS_REGISTRIES_CONF when registries.conf present."""
        store_dir = tmp_path / 'reg-store'
        main(['store', 'init', '--store-dir', str(store_dir), '--registry', 'mirror.example.com'])
        flags, env = _resolve_store_flags(str(store_dir))
        assert 'CONTAINERS_REGISTRIES_CONF' in env
        assert env['CONTAINERS_REGISTRIES_CONF'] == str(store_dir / 'registries.conf')

    def test_no_registries_conf_no_env(self, tmp_path, monkeypatch):
        """--store without registries.conf sets no env."""
        store_dir = tmp_path / 'no-reg-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        flags, env = _resolve_store_flags(str(store_dir))
        assert 'CONTAINERS_REGISTRIES_CONF' not in env

    def test_recreates_runroot_if_missing(self, tmp_path, monkeypatch):
        """--store recreates runroot if missing (post-reboot scenario)."""
        store_dir = tmp_path / 'reboot-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        # Simulate reboot: remove the runroot target
        graphroot_str = str(store_dir / 'graphroot')
        runroot = _runroot_path(graphroot_str)
        if os.path.isdir(runroot):
            os.rmdir(runroot)
        assert not os.path.isdir(runroot)
        flags, env = _resolve_store_flags(str(store_dir))
        assert os.path.isdir(runroot)

    def test_store_registry_wires_through(self, tmp_path, monkeypatch):
        """--store-registry wires through to registries.conf during auto-init."""
        store_dir = tmp_path / 'mirror-store'
        flags, env = _resolve_store_flags(
            str(store_dir), auto_init=True, registry='my-mirror.local'
        )
        reg_conf = store_dir / 'registries.conf'
        assert reg_conf.exists()
        assert 'my-mirror.local' in reg_conf.read_text()
        assert 'CONTAINERS_REGISTRIES_CONF' in env


class TestStoreMainIntegration:
    """Tests for --store flag integration in main()."""

    def test_print_cmd_contains_store_flags(self, tmp_path, monkeypatch, capsys, mock_run_os_cmd):
        """--store resolves to --root/--runroot/--storage-driver in --print-cmd output."""
        store_dir = tmp_path / 'cmd-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        capsys.readouterr()
        mock_run_os_cmd.set_return(returncode=1)
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--store', str(store_dir), '--no-devconfig', '--print-cmd', 'alpine'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert '--root' in out
        assert str(store_dir / 'graphroot') in out
        assert '--runroot' in out
        assert '--storage-driver' in out

    def test_store_conflicts_with_global_root(self, tmp_path, monkeypatch, capsys):
        """--store with --root in global flags errors."""
        store_dir = tmp_path / 'conflict-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        capsys.readouterr()
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    '--root',
                    '/some/path',
                    'run',
                    '--store',
                    str(store_dir),
                    '--no-devconfig',
                    '--print-cmd',
                    'alpine',
                ]
            )
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert '--store conflicts with --root' in err

    def test_store_conflicts_with_global_runroot(self, tmp_path, monkeypatch, capsys):
        """--store with --runroot in global flags errors."""
        store_dir = tmp_path / 'conflict-store2'
        main(['store', 'init', '--store-dir', str(store_dir)])
        capsys.readouterr()
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    '--runroot',
                    '/some/path',
                    'run',
                    '--store',
                    str(store_dir),
                    '--no-devconfig',
                    '--print-cmd',
                    'alpine',
                ]
            )
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert '--store conflicts with --runroot' in err

    def test_store_conflicts_with_global_storage_driver(self, tmp_path, monkeypatch, capsys):
        """--store with --storage-driver in global flags errors."""
        store_dir = tmp_path / 'conflict-store3'
        main(['store', 'init', '--store-dir', str(store_dir)])
        capsys.readouterr()
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    '--storage-driver',
                    'vfs',
                    'run',
                    '--store',
                    str(store_dir),
                    '--no-devconfig',
                    '--print-cmd',
                    'alpine',
                ]
            )
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert '--store conflicts with --storage-driver' in err

    def test_auto_init_store_without_store_errors(
        self, tmp_path, monkeypatch, capsys, mock_run_os_cmd
    ):
        """--auto-init-store without --store errors."""
        mock_run_os_cmd.set_return(returncode=1)
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--auto-init-store', '--no-devconfig', '--print-cmd', 'alpine'])
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert '--auto-init-store requires --store' in err

    def test_store_registry_without_auto_init_errors(
        self, tmp_path, monkeypatch, capsys, mock_run_os_cmd
    ):
        """--store-registry without --auto-init-store errors."""
        store_dir = tmp_path / 'reg-err-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        capsys.readouterr()
        mock_run_os_cmd.set_return(returncode=1)
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    'run',
                    '--store',
                    str(store_dir),
                    '--store-registry',
                    'mirror.local',
                    '--no-devconfig',
                    '--print-cmd',
                    'alpine',
                ]
            )
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert '--store-registry requires --auto-init-store' in err

    def test_store_sets_env_registries_conf(self, tmp_path, monkeypatch, capsys, mock_run_os_cmd):
        """--store sets CONTAINERS_REGISTRIES_CONF in os.environ when present."""
        store_dir = tmp_path / 'env-store'
        main(['store', 'init', '--store-dir', str(store_dir), '--registry', 'mirror.example.com'])
        capsys.readouterr()
        # Remove any pre-existing value
        monkeypatch.delenv('CONTAINERS_REGISTRIES_CONF', raising=False)
        mock_run_os_cmd.set_return(returncode=1)
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--store', str(store_dir), '--no-devconfig', '--print-cmd', 'alpine'])
        assert exc_info.value.code == 0
        assert os.environ.get('CONTAINERS_REGISTRIES_CONF') == str(store_dir / 'registries.conf')

    def test_auto_init_creates_store_via_main(self, tmp_path, monkeypatch, capsys, mock_run_os_cmd):
        """--store + --auto-init-store creates store on first use via main()."""
        store_dir = tmp_path / 'automain-store'
        mock_run_os_cmd.set_return(returncode=1)
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    'run',
                    '--store',
                    str(store_dir),
                    '--auto-init-store',
                    '--no-devconfig',
                    '--print-cmd',
                    'alpine',
                ]
            )
        assert exc_info.value.code == 0
        assert (store_dir / 'graphroot').is_dir()
        out = capsys.readouterr().out
        assert '--root' in out
        assert str(store_dir / 'graphroot') in out


class TestStoreDevconfig:
    """Tests for store/autoInitStore/storeRegistry in devcontainer.json."""

    def _write_devconfig(self, tmp_path, podrun_cfg, image='alpine'):
        """Write a devcontainer.json and chdir into tmp_path."""
        dc_dir = tmp_path / '.devcontainer'
        dc_dir.mkdir()
        dc = {'image': image, 'customizations': {'podrun': podrun_cfg}}
        (dc_dir / 'devcontainer.json').write_text(json.dumps(dc))

    def test_store_from_devconfig(self, tmp_path, monkeypatch, capsys, mock_run_os_cmd):
        """store in devcontainer.json resolves to --root/--runroot/--storage-driver."""
        store_dir = tmp_path / 'dc-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        capsys.readouterr()
        self._write_devconfig(tmp_path, {'store': str(store_dir)})
        monkeypatch.chdir(tmp_path)
        mock_run_os_cmd.set_return(returncode=1)
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--print-cmd'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert '--root' in out
        assert str(store_dir / 'graphroot') in out

    def test_auto_init_store_from_devconfig(self, tmp_path, monkeypatch, capsys, mock_run_os_cmd):
        """autoInitStore in devcontainer.json creates store on first use."""
        store_dir = tmp_path / 'dc-auto-store'
        self._write_devconfig(tmp_path, {'store': str(store_dir), 'autoInitStore': True})
        monkeypatch.chdir(tmp_path)
        mock_run_os_cmd.set_return(returncode=1)
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--print-cmd'])
        assert exc_info.value.code == 0
        assert (store_dir / 'graphroot').is_dir()
        out = capsys.readouterr().out
        assert '--root' in out

    def test_store_registry_from_devconfig(self, tmp_path, monkeypatch, capsys, mock_run_os_cmd):
        """storeRegistry in devcontainer.json wires through to registries.conf."""
        store_dir = tmp_path / 'dc-reg-store'
        self._write_devconfig(
            tmp_path,
            {'store': str(store_dir), 'autoInitStore': True, 'storeRegistry': 'mirror.local'},
        )
        monkeypatch.chdir(tmp_path)
        mock_run_os_cmd.set_return(returncode=1)
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--print-cmd'])
        assert exc_info.value.code == 0
        reg_conf = store_dir / 'registries.conf'
        assert reg_conf.exists()
        assert 'mirror.local' in reg_conf.read_text()

    def test_cli_store_overrides_devconfig(self, tmp_path, monkeypatch, capsys, mock_run_os_cmd):
        """CLI --store overrides store from devcontainer.json."""
        dc_store = tmp_path / 'dc-store'
        cli_store = tmp_path / 'cli-store'
        main(['store', 'init', '--store-dir', str(dc_store)])
        main(['store', 'init', '--store-dir', str(cli_store)])
        capsys.readouterr()
        self._write_devconfig(tmp_path, {'store': str(dc_store)})
        monkeypatch.chdir(tmp_path)
        mock_run_os_cmd.set_return(returncode=1)
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--store', str(cli_store), '--print-cmd'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert str(cli_store / 'graphroot') in out
        assert str(dc_store / 'graphroot') not in out

    def test_auto_init_store_without_store_in_devconfig_errors(
        self, tmp_path, monkeypatch, capsys, mock_run_os_cmd
    ):
        """autoInitStore without store in devcontainer.json errors."""
        self._write_devconfig(tmp_path, {'autoInitStore': True})
        monkeypatch.chdir(tmp_path)
        mock_run_os_cmd.set_return(returncode=1)
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--print-cmd'])
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert '--auto-init-store requires --store' in err

    def test_store_registry_without_auto_init_in_devconfig_errors(
        self, tmp_path, monkeypatch, capsys, mock_run_os_cmd
    ):
        """storeRegistry without autoInitStore in devcontainer.json errors."""
        store_dir = tmp_path / 'dc-reg-err'
        main(['store', 'init', '--store-dir', str(store_dir)])
        capsys.readouterr()
        self._write_devconfig(tmp_path, {'store': str(store_dir), 'storeRegistry': 'mirror.local'})
        monkeypatch.chdir(tmp_path)
        mock_run_os_cmd.set_return(returncode=1)
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--print-cmd'])
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert '--store-registry requires --auto-init-store' in err


class TestDiscoverStore:
    """Tests for _find_project_context() store discovery."""

    def test_finds_initialized_store(self, tmp_path):
        """Discovers store at .devcontainer/.podrun/store/graphroot/."""
        store = tmp_path / '.devcontainer' / '.podrun' / 'store'
        (store / 'graphroot').mkdir(parents=True)
        ctx = _find_project_context(start_dir=str(tmp_path))
        assert ctx.store_dir == str(store)

    def test_ignores_uninitialized(self, tmp_path):
        """No graphroot/ means no store discovered."""
        store = tmp_path / '.devcontainer' / '.podrun' / 'store'
        store.mkdir(parents=True)
        ctx = _find_project_context(start_dir=str(tmp_path))
        assert ctx.store_dir is None

    def test_walks_upward(self, tmp_path):
        """Finds store in parent directory."""
        store = tmp_path / '.devcontainer' / '.podrun' / 'store'
        (store / 'graphroot').mkdir(parents=True)
        child = tmp_path / 'sub' / 'dir'
        child.mkdir(parents=True)
        ctx = _find_project_context(start_dir=str(child))
        assert ctx.store_dir == str(store)

    def test_returns_none_when_absent(self, tmp_path):
        """No .devcontainer/.podrun/store anywhere → None."""
        child = tmp_path / 'empty'
        child.mkdir()
        ctx = _find_project_context(start_dir=str(child))
        assert ctx.store_dir is None

    def test_custom_start_dir(self, tmp_path):
        """Explicit start_dir is respected."""
        store = tmp_path / 'project' / '.devcontainer' / '.podrun' / 'store'
        (store / 'graphroot').mkdir(parents=True)
        ctx = _find_project_context(start_dir=str(tmp_path / 'project'))
        assert ctx.store_dir == str(store)


class TestHasStoreConflict:
    """Tests for _has_store_conflict()."""

    def test_no_conflict(self):
        assert not _has_store_conflict(['--log-level', 'debug'])

    def test_root_conflict(self):
        assert _has_store_conflict(['--root=/x'])

    def test_root_conflict_space(self):
        assert _has_store_conflict(['--root', '/x'])

    def test_runroot_conflict(self):
        assert _has_store_conflict(['--runroot=/x'])

    def test_storage_driver_conflict(self):
        assert _has_store_conflict(['--storage-driver', 'vfs'])

    def test_empty(self):
        assert not _has_store_conflict([])


class TestAutoDiscoveryIntegration:
    """Tests for auto-discovery integration in main()."""

    def _init_project_store(self, tmp_path, monkeypatch):
        """Create an initialized store at .devcontainer/.podrun/store and chdir."""
        store_dir = tmp_path / '.devcontainer' / '.podrun' / 'store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        monkeypatch.chdir(tmp_path)
        # Restore real _find_project_context (autouse fixture patches it out).
        monkeypatch.setattr(podrun_mod, '_find_project_context', _find_project_context)
        return store_dir

    def test_run_auto_discovers_store(self, tmp_path, monkeypatch, capsys, mock_run_os_cmd):
        """Auto-discovery injects store flags for run (via --print-cmd)."""
        store_dir = self._init_project_store(tmp_path, monkeypatch)
        capsys.readouterr()
        mock_run_os_cmd.set_return(returncode=1)
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--no-devconfig', '--print-cmd', 'alpine'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert '--root' in out
        assert str(store_dir / 'graphroot') in out

    def test_exec_auto_discovers_store(self, tmp_path, monkeypatch):
        """Auto-discovery injects store flags for exec."""
        store_dir = self._init_project_store(tmp_path, monkeypatch)
        execvpe_calls = []

        def fake_execvpe(*a):
            execvpe_calls.append(a)
            raise SystemExit(0)

        monkeypatch.setattr(podrun_mod.os, 'execvpe', fake_execvpe)
        with pytest.raises(SystemExit):
            main(['exec', 'mycontainer', 'ls'])
        assert len(execvpe_calls) == 1
        cmd = execvpe_calls[0][1]
        assert '--root' in cmd
        assert str(store_dir / 'graphroot') in cmd

    def test_passthrough_auto_discovers_store(self, tmp_path, monkeypatch):
        """Auto-discovery injects store flags for passthrough (e.g. ps)."""
        store_dir = self._init_project_store(tmp_path, monkeypatch)
        execvpe_calls = []

        def fake_execvpe(*a):
            execvpe_calls.append(a)
            raise SystemExit(0)

        monkeypatch.setattr(podrun_mod.os, 'execvpe', fake_execvpe)
        with pytest.raises(SystemExit):
            main(['ps', '-a'])
        assert len(execvpe_calls) == 1
        cmd = execvpe_calls[0][1]
        assert '--root' in cmd
        assert str(store_dir / 'graphroot') in cmd

    def test_cli_store_overrides_discovery(self, tmp_path, monkeypatch, capsys, mock_run_os_cmd):
        """CLI --store overrides auto-discovered store."""
        self._init_project_store(tmp_path, monkeypatch)
        cli_store = tmp_path / 'cli-store'
        main(['store', 'init', '--store-dir', str(cli_store)])
        capsys.readouterr()
        mock_run_os_cmd.set_return(returncode=1)
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--store', str(cli_store), '--no-devconfig', '--print-cmd', 'alpine'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert str(cli_store / 'graphroot') in out

    def test_devconfig_store_overrides_discovery(
        self, tmp_path, monkeypatch, capsys, mock_run_os_cmd
    ):
        """devcontainer.json store overrides auto-discovered store."""
        self._init_project_store(tmp_path, monkeypatch)
        dc_store = tmp_path / 'dc-store'
        main(['store', 'init', '--store-dir', str(dc_store)])
        capsys.readouterr()
        dc_dir = tmp_path / '.devcontainer'
        dc_dir.mkdir(exist_ok=True)
        (dc_dir / 'devcontainer.json').write_text(
            json.dumps(
                {
                    'image': 'alpine',
                    'customizations': {'podrun': {'store': str(dc_store)}},
                }
            )
        )
        mock_run_os_cmd.set_return(returncode=1)
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--print-cmd'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert str(dc_store / 'graphroot') in out

    def test_explicit_root_silently_skips_exec(self, tmp_path, monkeypatch):
        """Explicit --root in global flags silently skips discovery for exec."""
        self._init_project_store(tmp_path, monkeypatch)
        execvpe_calls = []

        def fake_execvpe(*a):
            execvpe_calls.append(a)
            raise SystemExit(0)

        monkeypatch.setattr(podrun_mod.os, 'execvpe', fake_execvpe)
        with pytest.raises(SystemExit):
            main(['--root=/custom', 'exec', 'mycontainer', 'ls'])
        assert len(execvpe_calls) == 1
        cmd = execvpe_calls[0][1]
        assert '--root=/custom' in cmd
        # Should NOT contain the discovered store's graphroot
        graphroot = str(tmp_path / '.devcontainer' / '.podrun' / 'store' / 'graphroot')
        assert graphroot not in cmd
