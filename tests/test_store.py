"""Tests for the ``podrun store`` subcommand."""

import os
import pathlib
import shutil
import subprocess
import sys

import pytest

import podrun.podrun as podrun_mod
from podrun.podrun import (
    PODMAN_SUBCOMMANDS,
    _PODRUN_SUBCOMMANDS,
    _detect_subcommand,
    _generate_store_activate,
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


class TestGenerateStoreActivate:
    def test_contains_path_manipulation(self, tmp_path):
        store_dir = tmp_path / 'store'
        store_dir.mkdir()
        bin_dir = store_dir / 'bin'
        bin_dir.mkdir()
        _generate_store_activate(store_dir, bin_dir, '/tmp/podrun-stores/abc')
        content = (store_dir / 'activate').read_text()
        assert 'PATH=' in content
        assert str(bin_dir) in content

    def test_contains_deactivate(self, tmp_path):
        store_dir = tmp_path / 'store'
        store_dir.mkdir()
        bin_dir = store_dir / 'bin'
        bin_dir.mkdir()
        _generate_store_activate(store_dir, bin_dir, '/tmp/podrun-stores/abc')
        content = (store_dir / 'activate').read_text()
        assert 'deactivate_podrun_store' in content

    def test_contains_ps1(self, tmp_path):
        store_dir = tmp_path / 'store'
        store_dir.mkdir()
        bin_dir = store_dir / 'bin'
        bin_dir.mkdir()
        _generate_store_activate(store_dir, bin_dir, '/tmp/podrun-stores/abc')
        content = (store_dir / 'activate').read_text()
        assert 'PS1=' in content
        assert '(podrun-store)' in content

    def test_no_xdg_config_home(self, tmp_path):
        store_dir = tmp_path / 'store'
        store_dir.mkdir()
        bin_dir = store_dir / 'bin'
        bin_dir.mkdir()
        _generate_store_activate(store_dir, bin_dir, '/tmp/podrun-stores/abc')
        content = (store_dir / 'activate').read_text()
        assert 'XDG_CONFIG_HOME' not in content

    def test_with_registries_conf(self, tmp_path):
        store_dir = tmp_path / 'store'
        store_dir.mkdir()
        bin_dir = store_dir / 'bin'
        bin_dir.mkdir()
        reg_conf = str(store_dir / 'registries.conf')
        _generate_store_activate(
            store_dir, bin_dir, '/tmp/podrun-stores/abc', registries_conf=reg_conf
        )
        content = (store_dir / 'activate').read_text()
        assert 'CONTAINERS_REGISTRIES_CONF' in content
        assert reg_conf in content

    def test_without_registries_conf(self, tmp_path):
        store_dir = tmp_path / 'store'
        store_dir.mkdir()
        bin_dir = store_dir / 'bin'
        bin_dir.mkdir()
        _generate_store_activate(store_dir, bin_dir, '/tmp/podrun-stores/abc')
        content = (store_dir / 'activate').read_text()
        assert 'CONTAINERS_REGISTRIES_CONF' not in content

    def test_contains_mkdir_runroot(self, tmp_path):
        store_dir = tmp_path / 'store'
        store_dir.mkdir()
        bin_dir = store_dir / 'bin'
        bin_dir.mkdir()
        target = '/tmp/podrun-stores/abc'
        _generate_store_activate(store_dir, bin_dir, target)
        content = (store_dir / 'activate').read_text()
        assert f'mkdir -p "{target}"' in content


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
        assert (store_dir / 'bin').is_dir()
        assert (store_dir / 'activate').exists()

    def test_runroot_is_symlink(self, tmp_path, monkeypatch):
        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        runroot = store_dir / 'runroot'
        assert runroot.is_symlink()
        target = os.readlink(str(runroot))
        assert target.startswith(podrun_mod._PODRUN_STORES_DIR)

    def test_bin_podman_has_flags(self, tmp_path, monkeypatch):
        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        content = (store_dir / 'bin' / 'podman').read_text()
        assert '--root' in content
        assert '--runroot' in content
        assert '--storage-driver' in content

    def test_bin_podrun_has_flags(self, tmp_path, monkeypatch):
        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        content = (store_dir / 'bin' / 'podrun').read_text()
        assert '--root' in content
        assert '--runroot' in content
        assert '--storage-driver' in content
        assert 'podrun.py' in content

    def test_bin_python3_is_symlink(self, tmp_path, monkeypatch):
        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        python_link = store_dir / 'bin' / 'python3'
        assert python_link.is_symlink()
        assert os.readlink(str(python_link)) == sys.executable

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
        assert 'activate' in out.lower()


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
        assert 'Activate with:' in out or 'Activated.' in out

    def test_nonexistent_store(self, tmp_path, capsys):
        store_dir = tmp_path / 'nonexistent'
        with pytest.raises(SystemExit) as exc_info:
            main(['store', 'info', '--store-dir', str(store_dir)])
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert 'No store found' in err
        assert 'podrun store init' in err

    def test_shows_activate_hint_when_inactive(self, tmp_path, monkeypatch, capsys):
        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        capsys.readouterr()
        main(['store', 'info', '--store-dir', str(store_dir)])
        out = capsys.readouterr().out
        assert 'Activate with:' in out
        assert 'Activated.' not in out

    def test_shows_active_when_bin_in_path(self, tmp_path, monkeypatch, capsys):
        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        capsys.readouterr()
        # Simulate activation by putting bin/ in PATH
        bin_dir = str(store_dir / 'bin')
        monkeypatch.setenv('PATH', bin_dir + os.pathsep + os.environ.get('PATH', ''))
        main(['store', 'info', '--store-dir', str(store_dir)])
        out = capsys.readouterr().out
        assert 'Activated.' in out
        assert 'Activate with:' not in out

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


class TestWrapperScriptQuoting:
    def test_paths_with_spaces_are_quoted(self, tmp_path, monkeypatch):
        """Wrapper scripts quote all paths so spaces don't break them."""
        store_dir = tmp_path / 'my store dir'
        store_dir.mkdir()
        monkeypatch.setattr(
            podrun_mod.shutil,
            'which',
            lambda x: '/usr/bin/my podman',
        )
        main(['store', 'init', '--store-dir', str(store_dir)])
        podman_content = (store_dir / 'bin' / 'podman').read_text()
        podrun_content = (store_dir / 'bin' / 'podrun').read_text()
        graphroot = str(store_dir / 'graphroot')
        # Verify paths are quoted in both wrappers
        assert f'--root "{graphroot}"' in podman_content
        assert f'--root "{graphroot}"' in podrun_content
        assert '"/usr/bin/my podman"' in podman_content


class TestActivateScriptFunctional:
    """Run the activate script in a real shell and verify behavior."""

    def _init_store(self, tmp_path, monkeypatch):
        """Helper to create a store and return its path."""
        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir)])
        return store_dir

    def test_activate_prepends_bin_to_path(self, tmp_path, monkeypatch):
        store_dir = self._init_store(tmp_path, monkeypatch)
        result = subprocess.run(
            ['bash', '-c', f'source "{store_dir}/activate" && echo "$PATH"'],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert str(store_dir / 'bin') in result.stdout.split(':')[0]

    def test_activate_sets_ps1(self, tmp_path, monkeypatch):
        store_dir = self._init_store(tmp_path, monkeypatch)
        result = subprocess.run(
            ['bash', '-c', f'PS1="$ " && source "{store_dir}/activate" && echo "$PS1"'],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert '(podrun-store)' in result.stdout

    def test_deactivate_restores_path(self, tmp_path, monkeypatch):
        store_dir = self._init_store(tmp_path, monkeypatch)
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
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert 'PATH_RESTORED' in result.stdout

    def test_deactivate_restores_ps1(self, tmp_path, monkeypatch):
        store_dir = self._init_store(tmp_path, monkeypatch)
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
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert result.stdout.strip() == 'original$'

    def test_activate_creates_runroot_dir(self, tmp_path, monkeypatch):
        """Activate recreates the /tmp runroot dir (post-reboot scenario)."""
        store_dir = self._init_store(tmp_path, monkeypatch)
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
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        assert os.path.exists(runroot_target)

    def test_registries_conf_set_and_restored(self, tmp_path, monkeypatch):
        """Activate sets CONTAINERS_REGISTRIES_CONF; deactivate restores it."""
        store_dir = tmp_path / 'test-store'
        main(['store', 'init', '--store-dir', str(store_dir), '--registry', 'mirror.example.com'])
        reg_path = str(store_dir / 'registries.conf')
        result = subprocess.run(
            [
                'bash',
                '-c',
                f'unset CONTAINERS_REGISTRIES_CONF && '
                f'source "{store_dir}/activate" && '
                f'echo "$CONTAINERS_REGISTRIES_CONF" && '
                f'deactivate_podrun_store && '
                f'echo "${{CONTAINERS_REGISTRIES_CONF:-unset}}"',
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f'stderr: {result.stderr}'
        lines = result.stdout.strip().splitlines()
        assert lines[0] == reg_path
        assert lines[1] == 'unset'


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
