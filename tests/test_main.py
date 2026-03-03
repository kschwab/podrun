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
