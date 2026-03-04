import os
import subprocess

import pytest

import podrun.podrun as podrun_mod
from podrun.podrun import _detect_subcommand, main


class TestMain:
    def test_no_image_errors(self, mock_run_os_cmd, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--no-devconfig'])
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert 'No image specified' in err

    def test_export_without_user_overlay_errors(self, mock_run_os_cmd, capsys):
        mock_run_os_cmd.set_return(returncode=1)
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--no-devconfig', '--export', '/opt/sdk:./sdk', 'alpine'])
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert '--export requires --user-overlay' in err

    def test_print_overlays_exits(self, mock_run_os_cmd, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--print-overlays', '--no-devconfig', 'alpine'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'Overlay' in out

    def test_devcontainer_with_config_flag(self, mock_run_os_cmd, tmp_path, capsys):
        """Cover main() --config path (lines 1080-1081)."""
        dc = tmp_path / 'dc.json'
        dc.write_text('{"image": "from-config"}')
        mock_run_os_cmd.set_return(returncode=1)  # no existing container
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--config', str(dc), '--print-cmd'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'from-config' in out

    def test_devcontainer_discovery(self, mock_run_os_cmd, tmp_path, capsys, monkeypatch):
        """Cover main() devcontainer discovery path (line 1083)."""
        dc_dir = tmp_path / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text('{"image": "discovered-image"}')
        monkeypatch.chdir(tmp_path)
        mock_run_os_cmd.set_return(returncode=1)
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--print-cmd'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'discovered-image' in out

    def test_handle_state_returns_none(self, mock_run_os_cmd, monkeypatch):
        """Cover main() action is None -> sys.exit(0) (line 1100)."""
        # Use a named container that's running, with auto_attach=False and auto_replace=False
        # handle_container_state will return None
        mock_run_os_cmd.set_return(stdout='running\n')
        monkeypatch.setattr(
            podrun_mod,
            'handle_container_state',
            lambda config, global_flags=None, podman_path='podman': None,
        )
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--no-devconfig', '--name', 'test', 'alpine'])
        assert exc_info.value.code == 0

    def test_attach_execvpe(self, mock_run_os_cmd, monkeypatch):
        """Cover main() attach os.execvpe path (unreachable by live tests)."""
        mock_run_os_cmd.set_side_effect(
            [
                subprocess.CompletedProcess(args='', returncode=0, stdout='running\n', stderr=''),
                subprocess.CompletedProcess(
                    args='', returncode=0, stdout='PODRUN_OVERLAYS=user\n', stderr=''
                ),
            ]
        )
        execvpe_calls = []

        def fake_execvpe(*a):
            execvpe_calls.append(a)
            raise SystemExit(0)

        monkeypatch.setattr(podrun_mod.os, 'execvpe', fake_execvpe)
        with pytest.raises(SystemExit):
            main(['run', '--no-devconfig', '--auto-attach', '--name', 'test', 'alpine'])
        assert len(execvpe_calls) == 1
        assert 'exec' in execvpe_calls[0][1]

    def test_nested_podrun_errors(self, monkeypatch, capsys):
        """Running podrun inside a podrun container (PODRUN_OVERLAYS set) → error."""
        monkeypatch.setenv('PODRUN_OVERLAYS', 'user,host')
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--no-devconfig', 'alpine'])
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert 'cannot be run inside a podrun container' in err

    def test_podman_not_found_errors(self, monkeypatch, capsys):
        """podman not in PATH → error."""
        monkeypatch.setattr(podrun_mod.shutil, 'which', lambda x: None)
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--no-devconfig', 'alpine'])
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert 'podman not found' in err

    def test_devcontainer_podman_path(self, mock_run_os_cmd, tmp_path, capsys, monkeypatch):
        """devcontainer.json podmanPath overrides default podman binary."""
        dc = tmp_path / 'dc.json'
        dc.write_text(
            '{"image": "alpine", "customizations": {"podrun": {"podmanPath": "/custom/podman"}}}'
        )
        # Make shutil.which resolve /custom/podman
        _real_which = podrun_mod.shutil.which
        monkeypatch.setattr(
            podrun_mod.shutil,
            'which',
            lambda x: '/custom/podman' if x == '/custom/podman' else _real_which(x),
        )
        mock_run_os_cmd.set_return(returncode=1)
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--config', str(dc), '--print-cmd'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert '/custom/podman' in out

    def test_devcontainer_podman_path_not_found(
        self, mock_run_os_cmd, tmp_path, capsys, monkeypatch
    ):
        """devcontainer.json podmanPath that doesn't resolve → error exit."""
        dc = tmp_path / 'dc.json'
        dc.write_text(
            '{"image": "alpine", "customizations": {"podrun": {"podmanPath": "/no/such/podman"}}}'
        )
        monkeypatch.setattr(
            podrun_mod.shutil,
            'which',
            lambda x: 'podman' if x == 'podman' else None,
        )
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--config', str(dc)])
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert 'podmanPath' in err
        assert '/no/such/podman' in err


class TestFuseOverlayfsInjection:
    """Test --fuse-overlayfs injects --storage-opt into the podman command."""

    def test_fuse_overlayfs_injects_storage_opt(self, mock_run_os_cmd, capsys, monkeypatch):
        """--fuse-overlayfs with fuse-overlayfs in PATH → --storage-opt in command."""
        mock_run_os_cmd.set_return(returncode=1)
        _real_which = podrun_mod.shutil.which
        monkeypatch.setattr(
            podrun_mod.shutil,
            'which',
            lambda x: '/usr/bin/fuse-overlayfs' if x == 'fuse-overlayfs' else _real_which(x),
        )
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--no-devconfig', '--fuse-overlayfs', '--print-cmd', 'alpine'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert '--storage-opt' in out
        assert 'overlay.mount_program=/usr/bin/fuse-overlayfs' in out

    def test_fuse_overlayfs_not_found_errors(self, mock_run_os_cmd, capsys, monkeypatch):
        """--fuse-overlayfs without fuse-overlayfs in PATH → error exit."""
        mock_run_os_cmd.set_return(returncode=1)
        _real_which = podrun_mod.shutil.which
        monkeypatch.setattr(
            podrun_mod.shutil,
            'which',
            lambda x: None if x == 'fuse-overlayfs' else _real_which(x),
        )
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--no-devconfig', '--fuse-overlayfs', '--print-cmd', 'alpine'])
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert '--fuse-overlayfs requested but fuse-overlayfs not found' in err

    def test_fuse_overlayfs_storage_opt_before_run_args(self, mock_run_os_cmd, capsys, monkeypatch):
        """--storage-opt from fuse-overlayfs appears as a global flag (before 'run')."""
        mock_run_os_cmd.set_return(returncode=1)
        _real_which = podrun_mod.shutil.which
        monkeypatch.setattr(
            podrun_mod.shutil,
            'which',
            lambda x: '/usr/bin/fuse-overlayfs' if x == 'fuse-overlayfs' else _real_which(x),
        )
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--no-devconfig', '--fuse-overlayfs', '--print-cmd', 'alpine'])
        assert exc_info.value.code == 0
        parts = capsys.readouterr().out.strip().split()
        storage_opt_idx = parts.index('--storage-opt')
        run_idx = parts.index('run')
        assert storage_opt_idx < run_idx

    def test_no_fuse_overlayfs_no_storage_opt(self, mock_run_os_cmd, capsys):
        """Without --fuse-overlayfs, no --storage-opt injected."""
        mock_run_os_cmd.set_return(returncode=1)
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--no-devconfig', '--print-cmd', 'alpine'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'overlay.mount_program' not in out

    def test_fuse_overlayfs_converts_overlay_file_mount(
        self, mock_run_os_cmd, capsys, monkeypatch, tmp_path
    ):
        """--fuse-overlayfs converts :O to :ro for single-file volume mounts."""
        mock_run_os_cmd.set_return(returncode=1)
        _real_which = podrun_mod.shutil.which
        monkeypatch.setattr(
            podrun_mod.shutil,
            'which',
            lambda x: '/usr/bin/fuse-overlayfs' if x == 'fuse-overlayfs' else _real_which(x),
        )
        # Create a real file so os.path.isfile returns True
        fake_file = tmp_path / '.gitconfig'
        fake_file.write_text('[user]\n')
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    'run',
                    '--no-devconfig',
                    '--fuse-overlayfs',
                    '--print-cmd',
                    f'-v={fake_file}:/home/user/.gitconfig:O',
                    'alpine',
                ]
            )
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        # :O should be converted to :ro
        assert f'-v={fake_file}:/home/user/.gitconfig:ro' in out
        assert ':O' not in out

    def test_fuse_overlayfs_preserves_overlay_dir_mount(
        self, mock_run_os_cmd, capsys, monkeypatch, tmp_path
    ):
        """--fuse-overlayfs preserves :O for directory volume mounts."""
        mock_run_os_cmd.set_return(returncode=1)
        _real_which = podrun_mod.shutil.which
        monkeypatch.setattr(
            podrun_mod.shutil,
            'which',
            lambda x: '/usr/bin/fuse-overlayfs' if x == 'fuse-overlayfs' else _real_which(x),
        )
        # Create a real directory so os.path.isfile returns False
        fake_dir = tmp_path / '.ssh'
        fake_dir.mkdir()
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    'run',
                    '--no-devconfig',
                    '--fuse-overlayfs',
                    '--print-cmd',
                    f'-v={fake_dir}:/home/user/.ssh:O',
                    'alpine',
                ]
            )
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        # :O should be preserved for directories
        assert f'-v={fake_dir}:/home/user/.ssh:O' in out


class TestRunOsCmd:
    def test_real_run_os_cmd(self):
        """Cover the real run_os_cmd function (lines 221-223).

        The autouse fixture only patches constants, not run_os_cmd itself,
        so calling podrun_mod.run_os_cmd exercises the real implementation.
        """
        result = podrun_mod.run_os_cmd('echo hello')
        assert result.returncode == 0
        assert 'hello' in result.stdout


class TestMainEntrypoint:
    def test_main_block_normal(self, monkeypatch):
        """Cover if __name__ == '__main__' block (lines 1131-1132)."""
        import runpy

        monkeypatch.setattr(
            'sys.argv', ['podrun', 'run', '--no-devconfig', '--print-cmd', 'alpine']
        )
        with pytest.raises(SystemExit) as exc_info:
            runpy.run_path(
                os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    'podrun',
                    'podrun.py',
                ),
                run_name='__main__',
            )
        assert exc_info.value.code == 0

    def test_main_block_keyboard_interrupt(self, monkeypatch):
        """Cover if __name__ == '__main__' KeyboardInterrupt (lines 1133-1134)."""
        import runpy

        def raise_kb(*args, **kwargs):
            raise KeyboardInterrupt()

        # Monkeypatch os.execvpe (globally shared) to raise KeyboardInterrupt.
        # runpy re-executes the file; main() will run, reach os.execvpe, raise KB,
        # which the __main__ block catches.
        monkeypatch.setattr('sys.argv', ['podrun', 'run', '--no-devconfig', 'alpine'])
        monkeypatch.setattr(os, 'execvpe', raise_kb)
        with pytest.raises(SystemExit) as exc_info:
            runpy.run_path(
                os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    'podrun',
                    'podrun.py',
                ),
                run_name='__main__',
            )
        assert 'KeyboardInterrupt' in str(exc_info.value)


class TestSubcommandRouting:
    """Test that main() routes subcommands correctly."""

    def test_explicit_run(self, mock_run_os_cmd, capsys):
        """podrun run --print-cmd --no-devconfig alpine → routes to _main_run."""
        mock_run_os_cmd.set_return(returncode=1)
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--print-cmd', '--no-devconfig', 'alpine'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'podman' in out
        assert 'alpine' in out

    @pytest.mark.parametrize(
        'cmd_args,expected_cmd',
        [
            (['version'], ['podman', 'version']),
            (
                ['version', '--format', '{{.Server.Version}}'],
                ['podman', 'version', '--format', '{{.Server.Version}}'],
            ),
            (['ps', '-a'], ['podman', 'ps', '-a']),
            (['inspect', 'abc123'], ['podman', 'inspect', 'abc123']),
            (['build', '.'], ['podman', 'build', '.']),
            (['exec', 'mycontainer', 'ls'], ['podman', 'exec', 'mycontainer', 'ls']),
            (['events', '--filter=event=start'], ['podman', 'events', '--filter=event=start']),
            (['stop', 'mycontainer'], ['podman', 'stop', 'mycontainer']),
            (['rm', '-f', 'mycontainer'], ['podman', 'rm', '-f', 'mycontainer']),
            (['--root=/x', 'ps'], ['podman', '--root=/x', 'ps']),
        ],
        ids=lambda p: p[0] if isinstance(p, list) else None,
    )
    def test_passthrough_subcommands(self, cmd_args, expected_cmd, monkeypatch):
        """Subcommands pass through to podman via execvpe."""
        execvpe_calls = []

        def fake_execvpe(*a):
            execvpe_calls.append(a)
            raise SystemExit(0)

        monkeypatch.setattr(podrun_mod.os, 'execvpe', fake_execvpe)
        with pytest.raises(SystemExit):
            main(cmd_args)
        assert len(execvpe_calls) == 1
        assert execvpe_calls[0][0] == 'podman'
        assert execvpe_calls[0][1] == expected_cmd

    def test_no_subcommand_passthrough(self, monkeypatch):
        """podrun --print-cmd --no-devconfig alpine → passthrough to podman (no implicit run)."""
        execvpe_calls = []

        def fake_execvpe(*a):
            execvpe_calls.append(a)
            raise SystemExit(0)

        monkeypatch.setattr(podrun_mod.os, 'execvpe', fake_execvpe)
        with pytest.raises(SystemExit):
            main(['--print-cmd', '--no-devconfig', 'alpine'])
        assert len(execvpe_calls) == 1
        assert execvpe_calls[0][0] == 'podman'
        assert execvpe_calls[0][1] == ['podman', '--print-cmd', '--no-devconfig', 'alpine']

    def test_explicit_run_with_global_flags(self, mock_run_os_cmd, capsys):
        """podrun --root=/x run --print-cmd --no-devconfig alpine → global flags before run."""
        mock_run_os_cmd.set_return(returncode=1)
        with pytest.raises(SystemExit) as exc_info:
            main(['--root=/x', 'run', '--print-cmd', '--no-devconfig', 'alpine'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'podman' in out
        assert 'alpine' in out
        # --root=/x must appear before 'run' in the output
        parts = out.strip().split()
        root_idx = parts.index('--root=/x')
        run_idx = parts.index('run')
        assert root_idx < run_idx, f'--root=/x ({root_idx}) should precede run ({run_idx})'

    def test_store_destroy_nonexistent(self, tmp_path, capsys):
        """podrun store destroy on nonexistent dir → error exit."""
        store_dir = tmp_path / 'nonexistent'
        with pytest.raises(SystemExit) as exc_info:
            main(['store', 'destroy', '--store-dir', str(store_dir)])
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert 'does not exist' in err

    def test_store_info_nonexistent(self, tmp_path, capsys):
        """podrun store info on nonexistent dir → error exit."""
        store_dir = tmp_path / 'nonexistent'
        with pytest.raises(SystemExit) as exc_info:
            main(['store', 'info', '--store-dir', str(store_dir)])
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert 'No store found' in err

    def test_store_no_action(self, capsys):
        """podrun store (no subaction) → help and exit 1."""
        with pytest.raises(SystemExit) as exc_info:
            main(['store'])
        assert exc_info.value.code == 1


class TestTopLevelHelp:
    """Test that podrun --help shows top-level help (podman --help + podrun commands)."""

    def test_top_level_help_podman_fails(self, mock_run_os_cmd, capsys):
        """podrun --help gracefully handles podman --help failure."""
        mock_run_os_cmd.set_return(returncode=1)
        with pytest.raises(SystemExit) as exc_info:
            main(['--help'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        # Fallback header is shown
        assert 'podrun' in out
        # Podrun sections still present
        assert 'Available Commands:' in out
        assert 'store' in out


class TestGlobalFlagsPassthrough:
    """Test that podman global flags (--root, --runroot, etc.) before the
    subcommand are forwarded correctly into the final podman command."""

    def test_run_print_cmd_single_global_flag(self, mock_run_os_cmd, capsys):
        """--root=/store run → podman --root=/store run ..."""
        mock_run_os_cmd.set_return(returncode=1)
        with pytest.raises(SystemExit) as exc_info:
            main(['--root=/store', 'run', '--print-cmd', '--no-devconfig', 'alpine'])
        assert exc_info.value.code == 0
        parts = capsys.readouterr().out.strip().split()
        assert parts[0] == 'podman'
        assert parts[1] == '--root=/store'
        assert parts[2] == 'run'

    def test_run_print_cmd_multiple_global_flags(self, mock_run_os_cmd, capsys):
        """--root=/store --runroot=/run run → both before run."""
        mock_run_os_cmd.set_return(returncode=1)
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    '--root=/store',
                    '--runroot=/run',
                    'run',
                    '--print-cmd',
                    '--no-devconfig',
                    'alpine',
                ]
            )
        assert exc_info.value.code == 0
        parts = capsys.readouterr().out.strip().split()
        assert parts[0] == 'podman'
        assert '--root=/store' in parts
        assert '--runroot=/run' in parts
        run_idx = parts.index('run')
        assert parts.index('--root=/store') < run_idx
        assert parts.index('--runroot=/run') < run_idx

    def test_run_print_cmd_space_separated_global_flag(self, mock_run_os_cmd, capsys):
        """--root /store run → podman --root /store run ..."""
        mock_run_os_cmd.set_return(returncode=1)
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    '--root',
                    '/store',
                    'run',
                    '--print-cmd',
                    '--no-devconfig',
                    'alpine',
                ]
            )
        assert exc_info.value.code == 0
        parts = capsys.readouterr().out.strip().split()
        assert parts[0] == 'podman'
        assert parts[1] == '--root'
        assert parts[2] == '/store'
        assert parts[3] == 'run'

    def test_run_print_cmd_storage_opt(self, mock_run_os_cmd, capsys):
        """--storage-opt ignore_chown_errors=true run → before run."""
        mock_run_os_cmd.set_return(returncode=1)
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    '--storage-opt',
                    'ignore_chown_errors=true',
                    'run',
                    '--print-cmd',
                    '--no-devconfig',
                    'alpine',
                ]
            )
        assert exc_info.value.code == 0
        parts = capsys.readouterr().out.strip().split()
        run_idx = parts.index('run')
        assert parts.index('--storage-opt') < run_idx
        assert parts.index('ignore_chown_errors=true') < run_idx

    def test_run_execvpe_global_flags_in_cmd(self, mock_run_os_cmd, monkeypatch):
        """Global flags appear in the execvpe argv before 'run'."""
        mock_run_os_cmd.set_return(returncode=1)
        execvpe_calls = []

        def fake_execvpe(*a):
            execvpe_calls.append(a)
            raise SystemExit(0)

        monkeypatch.setattr(podrun_mod.os, 'execvpe', fake_execvpe)
        with pytest.raises(SystemExit):
            main(['--root=/store', 'run', '--no-devconfig', 'alpine'])
        assert len(execvpe_calls) == 1
        cmd = execvpe_calls[0][1]
        assert cmd[0] == 'podman'
        assert cmd[1] == '--root=/store'
        assert cmd[2] == 'run'

    def test_exec_global_flags(self, monkeypatch):
        """podrun --root=/store exec mycontainer ls → flags before exec."""
        execvpe_calls = []

        def fake_execvpe(*a):
            execvpe_calls.append(a)
            raise SystemExit(0)

        monkeypatch.setattr(podrun_mod.os, 'execvpe', fake_execvpe)
        with pytest.raises(SystemExit):
            main(['--root=/store', 'exec', 'mycontainer', 'ls'])
        assert len(execvpe_calls) == 1
        cmd = execvpe_calls[0][1]
        assert cmd == ['podman', '--root=/store', 'exec', 'mycontainer', 'ls']

    def test_exec_multiple_global_flags(self, monkeypatch):
        """podrun --root=/s --runroot=/r exec ctr ls → both before exec."""
        execvpe_calls = []

        def fake_execvpe(*a):
            execvpe_calls.append(a)
            raise SystemExit(0)

        monkeypatch.setattr(podrun_mod.os, 'execvpe', fake_execvpe)
        with pytest.raises(SystemExit):
            main(['--root=/s', '--runroot=/r', 'exec', 'mycontainer', 'ls'])
        assert len(execvpe_calls) == 1
        cmd = execvpe_calls[0][1]
        assert cmd == ['podman', '--root=/s', '--runroot=/r', 'exec', 'mycontainer', 'ls']

    def test_no_subcommand_passthrough_to_podman(self, monkeypatch):
        """No subcommand → passthrough to podman, no implicit run."""
        execvpe_calls = []

        def fake_execvpe(*a):
            execvpe_calls.append(a)
            raise SystemExit(0)

        monkeypatch.setattr(podrun_mod.os, 'execvpe', fake_execvpe)
        with pytest.raises(SystemExit):
            main(['--print-cmd', '--no-devconfig', 'alpine'])
        assert len(execvpe_calls) == 1
        assert execvpe_calls[0][1] == ['podman', '--print-cmd', '--no-devconfig', 'alpine']

    def test_global_flags_with_user_overlay(self, mock_run_os_cmd, capsys, podrun_tmp):
        """Global flags + user overlay → flags before run, overlay flags after."""
        mock_run_os_cmd.set_side_effect(
            [
                subprocess.CompletedProcess(args='', returncode=1, stdout='', stderr=''),
                subprocess.CompletedProcess(args='', returncode=0, stdout='Intel\n', stderr=''),
                subprocess.CompletedProcess(args='', returncode=0, stdout='8\n', stderr=''),
                subprocess.CompletedProcess(args='', returncode=0, stdout='', stderr=''),
            ]
        )
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    '--root=/store',
                    'run',
                    '--no-devconfig',
                    '--user-overlay',
                    '--print-cmd',
                    'alpine',
                ]
            )
        assert exc_info.value.code == 0
        parts = capsys.readouterr().out.strip().split()
        assert parts[0] == 'podman'
        assert parts[1] == '--root=/store'
        assert parts[2] == 'run'
        assert '--userns=keep-id' in parts


class TestPrintCmdContainerState:
    """End-to-end tests through main() with --print-cmd for container state handling.

    Verifies that --print-cmd allows prompts (on stderr) when no auto flag is set,
    uses defaults in non-interactive mode, and avoids side effects (no podman start, no rm).
    """

    @staticmethod
    def _has_podman_subcmd(calls, subcmd):
        """Check if any mock_run_os_cmd call contains subcmd as a discrete podman token."""
        for call in calls:
            tokens = call.split()
            if subcmd in tokens:
                return True
        return False

    def test_running_prints_exec(self, mock_run_os_cmd, capsys, monkeypatch):
        """Running container + --print-cmd (no explicit auto flags) → prompts on stderr, prints exec on stdout."""
        mock_run_os_cmd.set_side_effect(
            [
                subprocess.CompletedProcess(args='', returncode=0, stdout='running\n', stderr=''),
                subprocess.CompletedProcess(
                    args='', returncode=0, stdout='PODRUN_OVERLAYS=user\n', stderr=''
                ),
            ]
        )
        monkeypatch.setattr(
            'builtins.input',
            lambda *a: (_ for _ in ()).throw(AssertionError('input() should not be called')),
        )
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--no-devconfig', '--print-cmd', '--name', 'test', 'alpine'])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert 'exec' in captured.out
        assert 'Attach to already running instance?' in captured.err
        assert not self._has_podman_subcmd(mock_run_os_cmd.calls, 'start')
        assert not self._has_podman_subcmd(mock_run_os_cmd.calls, 'rm')

    def test_stopped_no_action(self, mock_run_os_cmd, capsys, monkeypatch):
        """Stopped container + --print-cmd (no explicit auto flags) → non-interactive defaults to no replace, exits 0."""
        mock_run_os_cmd.set_return(stdout='exited\n')
        monkeypatch.setattr(
            'builtins.input',
            lambda *a: (_ for _ in ()).throw(AssertionError('input() should not be called')),
        )
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--no-devconfig', '--print-cmd', '--name', 'test', 'alpine'])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert 'Replace stopped instance?' in captured.err
        assert not self._has_podman_subcmd(mock_run_os_cmd.calls, 'start')
        assert not self._has_podman_subcmd(mock_run_os_cmd.calls, 'rm')

    def test_none_prints_run(self, mock_run_os_cmd, capsys):
        """No container + --print-cmd → prints run cmd, exit 0."""
        mock_run_os_cmd.set_return(returncode=1)
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--no-devconfig', '--print-cmd', '--name', 'test', 'alpine'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'run' in out

    def test_running_no_prompt(self, mock_run_os_cmd, monkeypatch):
        """Running container + --print-cmd (non-interactive) → input() is never called."""
        mock_run_os_cmd.set_side_effect(
            [
                subprocess.CompletedProcess(args='', returncode=0, stdout='running\n', stderr=''),
                subprocess.CompletedProcess(
                    args='', returncode=0, stdout='PODRUN_OVERLAYS=user\n', stderr=''
                ),
            ]
        )
        called = []
        monkeypatch.setattr('builtins.input', lambda *a: called.append(1) or 'y')
        with pytest.raises(SystemExit):
            main(['run', '--no-devconfig', '--print-cmd', '--name', 'test', 'alpine'])
        assert len(called) == 0

    def test_running_explicit_auto_attach(self, mock_run_os_cmd, capsys):
        """Running + --print-cmd + --auto-attach → prints exec, no start."""
        mock_run_os_cmd.set_side_effect(
            [
                subprocess.CompletedProcess(args='', returncode=0, stdout='running\n', stderr=''),
                subprocess.CompletedProcess(
                    args='', returncode=0, stdout='PODRUN_OVERLAYS=user\n', stderr=''
                ),
            ]
        )
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    'run',
                    '--no-devconfig',
                    '--print-cmd',
                    '--auto-attach',
                    '--name',
                    'test',
                    'alpine',
                ]
            )
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'exec' in out
        assert not self._has_podman_subcmd(mock_run_os_cmd.calls, 'start')

    def test_stopped_explicit_auto_attach_warns(self, mock_run_os_cmd, capsys):
        """Stopped + --print-cmd + --auto-attach → warns, exits 0 (no action)."""
        mock_run_os_cmd.set_return(stdout='exited\n')
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    'run',
                    '--no-devconfig',
                    '--print-cmd',
                    '--auto-attach',
                    '--name',
                    'test',
                    'alpine',
                ]
            )
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert 'cannot auto-attach to container' in captured.err.lower()
        assert not self._has_podman_subcmd(mock_run_os_cmd.calls, 'start')
        assert not self._has_podman_subcmd(mock_run_os_cmd.calls, 'rm')

    def test_running_explicit_auto_replace_prints_rm_and_run(self, mock_run_os_cmd, capsys):
        """Running + --print-cmd + --auto-replace → prints rm + run, no side effects."""
        mock_run_os_cmd.set_return(stdout='running\n')
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    'run',
                    '--no-devconfig',
                    '--print-cmd',
                    '--auto-replace',
                    '--name',
                    'test',
                    'alpine',
                ]
            )
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        lines = out.strip().splitlines()
        assert 'rm' in lines[0]
        assert 'run' in lines[1]
        assert not self._has_podman_subcmd(mock_run_os_cmd.calls, 'rm')

    def test_stopped_explicit_auto_replace_prints_rm_and_run(self, mock_run_os_cmd, capsys):
        """Stopped + --print-cmd + --auto-replace → prints rm + run, no side effects."""
        mock_run_os_cmd.set_return(stdout='exited\n')
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    'run',
                    '--no-devconfig',
                    '--print-cmd',
                    '--auto-replace',
                    '--name',
                    'test',
                    'alpine',
                ]
            )
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        lines = out.strip().splitlines()
        assert 'rm' in lines[0]
        assert 'run' in lines[1]
        assert not self._has_podman_subcmd(mock_run_os_cmd.calls, 'rm')


class TestContainerStateWithoutPrintCmd:
    """End-to-end tests through main() WITHOUT --print-cmd.

    Verifies that real side effects (execvpe, podman rm) occur for auto flag
    combinations.  Stopped containers cannot be attached to — only replaced.
    """

    @staticmethod
    def _has_podman_subcmd(calls, subcmd):
        """Check if any mock_run_os_cmd call contains subcmd as a discrete podman token."""
        for call in calls:
            tokens = call.split()
            if subcmd in tokens:
                return True
        return False

    def test_running_auto_attach_execvpe_exec(self, mock_run_os_cmd, monkeypatch):
        """Running + --auto-attach → execvpe with exec cmd."""
        mock_run_os_cmd.set_side_effect(
            [
                subprocess.CompletedProcess(args='', returncode=0, stdout='running\n', stderr=''),
                subprocess.CompletedProcess(
                    args='', returncode=0, stdout='PODRUN_OVERLAYS=user\n', stderr=''
                ),
            ]
        )
        execvpe_calls = []

        def fake_execvpe(*a):
            execvpe_calls.append(a)
            raise SystemExit(0)

        monkeypatch.setattr(podrun_mod.os, 'execvpe', fake_execvpe)
        with pytest.raises(SystemExit):
            main(['run', '--no-devconfig', '--auto-attach', '--name', 'test', 'alpine'])
        assert len(execvpe_calls) == 1
        assert 'exec' in execvpe_calls[0][1]

    def test_stopped_auto_attach_warns_exits(self, mock_run_os_cmd, capsys):
        """Stopped + --auto-attach → warns can't attach, exits 0 (no action)."""
        mock_run_os_cmd.set_return(stdout='exited\n')
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--no-devconfig', '--auto-attach', '--name', 'test', 'alpine'])
        assert exc_info.value.code == 0
        assert 'cannot auto-attach to container' in capsys.readouterr().err.lower()

    def test_running_auto_replace_rm_then_run(self, mock_run_os_cmd, monkeypatch):
        """Running + --auto-replace → rm called, then execvpe with run cmd."""
        mock_run_os_cmd.set_side_effect(
            [
                subprocess.CompletedProcess(args='', returncode=0, stdout='running\n', stderr=''),
                subprocess.CompletedProcess(args='', returncode=0, stdout='', stderr=''),  # rm
            ]
        )
        execvpe_calls = []

        def fake_execvpe(*a):
            execvpe_calls.append(a)
            raise SystemExit(0)

        monkeypatch.setattr(podrun_mod.os, 'execvpe', fake_execvpe)
        with pytest.raises(SystemExit):
            main(['run', '--no-devconfig', '--auto-replace', '--name', 'test', 'alpine'])
        assert self._has_podman_subcmd(mock_run_os_cmd.calls, 'rm')
        assert len(execvpe_calls) == 1
        assert 'run' in execvpe_calls[0][1]

    def test_stopped_auto_replace_rm_then_run(self, mock_run_os_cmd, monkeypatch):
        """Stopped + --auto-replace → rm called, then execvpe with run cmd."""
        mock_run_os_cmd.set_side_effect(
            [
                subprocess.CompletedProcess(args='', returncode=0, stdout='exited\n', stderr=''),
                subprocess.CompletedProcess(args='', returncode=0, stdout='', stderr=''),  # rm
            ]
        )
        execvpe_calls = []

        def fake_execvpe(*a):
            execvpe_calls.append(a)
            raise SystemExit(0)

        monkeypatch.setattr(podrun_mod.os, 'execvpe', fake_execvpe)
        with pytest.raises(SystemExit):
            main(['run', '--no-devconfig', '--auto-replace', '--name', 'test', 'alpine'])
        assert self._has_podman_subcmd(mock_run_os_cmd.calls, 'rm')
        assert len(execvpe_calls) == 1
        assert 'run' in execvpe_calls[0][1]

    def test_none_execvpe_run(self, mock_run_os_cmd, monkeypatch):
        """No container → execvpe with run cmd."""
        mock_run_os_cmd.set_return(returncode=1)
        execvpe_calls = []

        def fake_execvpe(*a):
            execvpe_calls.append(a)
            raise SystemExit(0)

        monkeypatch.setattr(podrun_mod.os, 'execvpe', fake_execvpe)
        with pytest.raises(SystemExit):
            main(['run', '--no-devconfig', '--name', 'test', 'alpine'])
        assert len(execvpe_calls) == 1
        assert 'run' in execvpe_calls[0][1]

    def test_running_both_flags_attach_wins(self, mock_run_os_cmd, monkeypatch):
        """Running + --auto-attach + --auto-replace → attach wins: exec cmd, no rm."""
        mock_run_os_cmd.set_side_effect(
            [
                subprocess.CompletedProcess(args='', returncode=0, stdout='running\n', stderr=''),
                subprocess.CompletedProcess(
                    args='', returncode=0, stdout='PODRUN_OVERLAYS=user\n', stderr=''
                ),
            ]
        )
        execvpe_calls = []

        def fake_execvpe(*a):
            execvpe_calls.append(a)
            raise SystemExit(0)

        monkeypatch.setattr(podrun_mod.os, 'execvpe', fake_execvpe)
        with pytest.raises(SystemExit):
            main(
                [
                    'run',
                    '--no-devconfig',
                    '--auto-attach',
                    '--auto-replace',
                    '--name',
                    'test',
                    'alpine',
                ]
            )
        assert len(execvpe_calls) == 1
        assert 'exec' in execvpe_calls[0][1]
        assert not self._has_podman_subcmd(mock_run_os_cmd.calls, 'rm')

    def test_stopped_both_flags_replace_wins(self, mock_run_os_cmd, monkeypatch, capsys):
        """Stopped + --auto-attach + --auto-replace → can't attach, falls through to replace (rm + run)."""
        mock_run_os_cmd.set_side_effect(
            [
                subprocess.CompletedProcess(args='', returncode=0, stdout='exited\n', stderr=''),
                subprocess.CompletedProcess(args='', returncode=0, stdout='', stderr=''),  # rm
            ]
        )
        execvpe_calls = []

        def fake_execvpe(*a):
            execvpe_calls.append(a)
            raise SystemExit(0)

        monkeypatch.setattr(podrun_mod.os, 'execvpe', fake_execvpe)
        with pytest.raises(SystemExit):
            main(
                [
                    'run',
                    '--no-devconfig',
                    '--auto-attach',
                    '--auto-replace',
                    '--name',
                    'test',
                    'alpine',
                ]
            )
        assert 'cannot auto-attach to container' in capsys.readouterr().err.lower()
        assert self._has_podman_subcmd(mock_run_os_cmd.calls, 'rm')
        assert len(execvpe_calls) == 1
        assert 'run' in execvpe_calls[0][1]


class TestDetectSubcommand:
    """Test _detect_subcommand edge cases."""

    @pytest.mark.parametrize(
        'argv,expected',
        [
            (['run', 'alpine'], ('run', 0)),
            (['ps', '-a'], ('ps', 0)),
            (['exec', 'container', 'ls'], ('exec', 0)),
            (['version'], ('version', 0)),
            (['build', '.'], ('build', 0)),
            (['inspect', 'abc123'], ('inspect', 0)),
            (['store', 'init'], ('store', 0)),
            (['--root=/x', 'ps'], ('ps', 1)),
            (['--root', '/x', 'run', 'alpine'], ('run', 2)),
            (['--remote', 'ps'], ('ps', 1)),
            (['--root', '/x', '--log-level', 'debug', 'ps'], ('ps', 4)),
            (['--storage-opt', 'ignore_chown_errors=true', 'run', 'alpine'], ('run', 2)),
            (['--root=/x', 'store', 'init'], ('store', 1)),
            (['--user-overlay', 'alpine'], (None, 0)),
            ([], (None, 0)),
            (['alpine'], (None, 0)),
            (['--', 'run', 'alpine'], (None, 0)),
        ],
        ids=lambda p: str(p) if isinstance(p, list) else None,
    )
    def test_detect(self, argv, expected):
        assert _detect_subcommand(argv) == expected
