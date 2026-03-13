import subprocess

import pytest

from podrun.podrun2 import (
    PodmanFlags,
    _scrape_podman_help,
    build_passthrough_command,
    build_root_parser,
    build_run_command,
    build_store_command,
    is_podman_remote,
    load_podman_flags,
    main,
    parse_args,
    print_completion,
    print_help,
    print_version,
    run_os_cmd,
)

import podrun.podrun2 as podrun2_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_from_filesystem(monkeypatch):
    """Prevent CLI tests from picking up real devcontainer.json or store dirs."""
    monkeypatch.setattr(podrun2_mod, 'find_project_context', lambda start_dir=None: (None, None))


@pytest.fixture
def mock_run_os_cmd(monkeypatch):
    """Monkeypatch podrun2.run_os_cmd and return a controller.

    Only used for tests that need to simulate podman failure or control
    exact output.  Most tests use real podman.
    """

    class Controller:
        def __init__(self):
            self.calls = []
            self._return_value = None
            self._side_effect = None

        def set_return(self, stdout='', stderr='', returncode=0):
            self._return_value = subprocess.CompletedProcess(
                args='', returncode=returncode, stdout=stdout, stderr=stderr
            )
            self._side_effect = None

        def set_side_effect(self, effects):
            self._side_effect = list(effects)
            self._return_value = None

        def __call__(self, cmd):
            self.calls.append(cmd)
            if self._side_effect is not None:
                if self._side_effect:
                    val = self._side_effect.pop(0)
                else:
                    val = subprocess.CompletedProcess(args='', returncode=0, stdout='', stderr='')
                if isinstance(val, subprocess.CompletedProcess):
                    return val
                raise val
            if self._return_value is not None:
                return self._return_value
            return subprocess.CompletedProcess(args='', returncode=0, stdout='', stderr='')

    ctrl = Controller()
    monkeypatch.setattr(podrun2_mod, 'run_os_cmd', ctrl)
    return ctrl


# ---------------------------------------------------------------------------
# TestStoreFlag — parse-time config only, resolution deferred to Phase 2
# ---------------------------------------------------------------------------


class TestStoreFlag:
    def test_store_stores_config_value(self):
        """--store /path stores the config value; no podman flag translation at parse time."""
        r = parse_args(['--store', '/my/store', 'run', 'alpine'])
        assert r.ns['root.store'] == '/my/store'

    def test_store_equals_syntax(self):
        r = parse_args(['--store=/my/store', 'run', 'alpine'])
        assert r.ns['root.store'] == '/my/store'

    def test_store_before_passthrough(self):
        """--store before a passthrough subcommand stores config value."""
        r = parse_args(['--store', '/my/store', 'ps', '-a'])
        assert r.ns['root.store'] == '/my/store'
        assert r.ns['subcommand'] == 'ps'

    def test_store_with_podman_global(self):
        """--store and --log-level are independent; --log-level goes to podman_global_args."""
        r = parse_args(['--store', '/s', '--log-level', 'debug', 'run', 'alpine'])
        assert r.ns['root.store'] == '/s'
        pga = r.ns.get('podman_global_args') or []
        assert '--log-level' in pga
        assert 'debug' in pga

    def test_store_default_none(self):
        r = parse_args(['run', 'alpine'])
        assert r.ns['root.store'] is None


# ---------------------------------------------------------------------------
# TestSubcommandRouting — argparse subparsers handle routing
# ---------------------------------------------------------------------------


class TestSubcommandRouting:
    """Verify that the root parser's subparsers correctly identify subcommands."""

    def test_explicit_run(self):
        r = parse_args(['run', 'alpine'])
        assert r.ns['subcommand'] == 'run'

    def test_ps(self):
        r = parse_args(['ps', '-a'])
        assert r.ns['subcommand'] == 'ps'

    def test_exec(self):
        r = parse_args(['exec', 'container', 'ls'])
        assert r.ns['subcommand'] == 'exec'

    def test_version_subcommand(self):
        """'version' as a subcommand (not --version flag)."""
        r = parse_args(['version'])
        assert r.ns['subcommand'] == 'version'

    def test_build(self):
        r = parse_args(['build', '.'])
        assert r.ns['subcommand'] == 'build'

    def test_inspect(self):
        r = parse_args(['inspect', 'abc123'])
        assert r.ns['subcommand'] == 'inspect'

    def test_store(self):
        r = parse_args(['store', 'init'])
        assert r.ns['subcommand'] == 'store'

    def test_global_flags_before_subcommand(self):
        """Root parser consumes --root /x, then 'ps' routes correctly."""
        r = parse_args(['--root', '/x', 'ps'])
        assert r.ns['subcommand'] == 'ps'
        pga = r.ns.get('podman_global_args') or []
        assert '--root' in pga
        assert '/x' in pga

    def test_global_equals_before_subcommand(self):
        r = parse_args(['--root=/x', 'ps'])
        assert r.ns['subcommand'] == 'ps'
        pga = r.ns.get('podman_global_args') or []
        assert '--root' in pga
        assert '/x' in pga

    def test_multiple_global_flags(self):
        r = parse_args(['--root', '/x', '--log-level', 'debug', 'ps'])
        assert r.ns['subcommand'] == 'ps'
        pga = r.ns.get('podman_global_args') or []
        assert '--root' in pga
        assert '--log-level' in pga

    def test_storage_opt_before_run(self):
        r = parse_args(['--storage-opt', 'ignore_chown_errors=true', 'run', 'alpine'])
        assert r.ns['subcommand'] == 'run'
        pga = r.ns.get('podman_global_args') or []
        assert '--storage-opt' in pga

    def test_podrun_global_value_flag_before_subcommand(self):
        """--store is a podrun global value flag — argparse consumes it,
        then correctly routes to the 'run' subparser."""
        r = parse_args(['--store', '/my/store', 'run', 'alpine'])
        assert r.ns['subcommand'] == 'run'
        assert r.ns['root.store'] == '/my/store'
        assert 'alpine' in r.trailing_args

    def test_remote_boolean_global_flag(self):
        """--remote is a podman global boolean flag, consumed by root parser."""
        r = parse_args(['--remote', 'ps', '-a'])
        assert r.ns['subcommand'] == 'ps'
        pga = r.ns.get('podman_global_args') or []
        assert '--remote' in pga

    def test_no_subcommand(self):
        """No subcommand → subcommand is None."""
        r = parse_args(['--version'])
        assert r.ns['subcommand'] is None

    def test_empty_argv(self):
        r = parse_args([])
        assert r.ns['subcommand'] is None

    def test_separator_stops_subcommand_detection(self):
        """'run' after -- is part of explicit_command, not a subcommand."""
        r = parse_args(['run', 'alpine', '--', 'run', 'something'])
        assert r.ns['subcommand'] == 'run'
        assert r.explicit_command == ['run', 'something']


# ---------------------------------------------------------------------------
# TestBuildRootParser
# ---------------------------------------------------------------------------


class TestBuildRootParser:
    def _parse(self, args):
        parser = build_root_parser()
        ns, unknowns = parser.parse_known_args(args)
        return vars(ns), unknowns

    def test_print_cmd_flag(self):
        ns, _ = self._parse(['--print-cmd'])
        assert ns['root.print_cmd'] is True

    def test_dry_run_alias(self):
        ns, _ = self._parse(['--dry-run'])
        assert ns['root.print_cmd'] is True

    def test_config_flag(self):
        ns, _ = self._parse(['--config', '/path/to/config.json'])
        assert ns['root.config'] == '/path/to/config.json'

    def test_no_devconfig_flag(self):
        ns, _ = self._parse(['--no-devconfig'])
        assert ns['root.no_devconfig'] is True

    def test_config_script_flag(self):
        ns, _ = self._parse(['--config-script', '/path/to/script'])
        assert ns['root.config_script'] == ['/path/to/script']

    def test_config_script_repeated(self):
        ns, _ = self._parse(['--config-script', '/a.sh', '--config-script', '/b.sh'])
        assert ns['root.config_script'] == ['/a.sh', '/b.sh']

    def test_completion_flag(self):
        ns, _ = self._parse(['--completion', 'bash'])
        assert ns['root.completion'] == 'bash'

    def test_version_flag(self):
        ns, _ = self._parse(['--version'])
        assert ns['root.version'] is True

    def test_version_short_flag(self):
        ns, _ = self._parse(['-v'])
        assert ns['root.version'] is True

    def test_store_flag(self):
        ns, _ = self._parse(['--store', '/my/store'])
        assert ns['root.store'] == '/my/store'

    def test_ignore_store_flag(self):
        ns, _ = self._parse(['--ignore-store'])
        assert ns['root.ignore_store'] is True

    def test_auto_init_store_flag(self):
        ns, _ = self._parse(['--auto-init-store'])
        assert ns['root.auto_init_store'] is True

    def test_store_registry_flag(self):
        ns, _ = self._parse(['--store-registry', 'mirror.example.com'])
        assert ns['root.store.registry'] == 'mirror.example.com'

    def test_podman_global_value_flags_consumed(self):
        """Podman global value flags are consumed into podman_global_args."""
        ns, unknowns = self._parse(['--root', '/x', '--log-level', 'debug'])
        args = ns.get('podman_global_args') or []
        assert '--root' in args
        assert '/x' in args
        assert '--log-level' in args
        assert 'debug' in args
        assert '--root' not in unknowns

    def test_podman_global_boolean_flags_consumed(self):
        """Podman global boolean flags are consumed into podman_global_args."""
        ns, unknowns = self._parse(['--remote'])
        args = ns.get('podman_global_args') or []
        assert '--remote' in args
        assert '--remote' not in unknowns

    def test_unknown_flags_pass_through(self):
        _, unknowns = self._parse(['--some-unknown-flag'])
        assert '--some-unknown-flag' in unknowns

    def test_defaults(self):
        ns, _ = self._parse([])
        assert ns['root.print_cmd'] is False
        assert ns['root.config'] is None
        assert ns['root.no_devconfig'] is False
        assert ns['root.config_script'] is None
        assert ns['root.completion'] is None
        assert ns['root.version'] is False
        assert ns['root.store'] is None
        assert ns['root.ignore_store'] is False
        assert ns['root.auto_init_store'] is False
        assert ns['root.store.registry'] is None


# ---------------------------------------------------------------------------
# TestBuildRunParser — test run subparser through root parser
# ---------------------------------------------------------------------------


class TestBuildRunParser:
    """Test run flags via root parser parse_known_args(['run', ...])."""

    def _parse_run(self, args):
        parser = build_root_parser()
        ns, unknowns = parser.parse_known_args(['run'] + args)
        return vars(ns), unknowns

    def test_name_flag(self):
        ns, _ = self._parse_run(['--name', 'mycontainer'])
        assert ns['run.name'] == 'mycontainer'

    def test_user_overlay(self):
        ns, _ = self._parse_run(['--user-overlay'])
        assert ns['run.user_overlay'] is True

    def test_host_overlay(self):
        ns, _ = self._parse_run(['--host-overlay'])
        assert ns['run.host_overlay'] is True

    def test_interactive_overlay(self):
        ns, _ = self._parse_run(['--interactive-overlay'])
        assert ns['run.interactive_overlay'] is True

    def test_workspace(self):
        ns, _ = self._parse_run(['--workspace'])
        assert ns['run.workspace'] is True

    def test_adhoc(self):
        ns, _ = self._parse_run(['--adhoc'])
        assert ns['run.adhoc'] is True

    def test_print_overlays(self):
        ns, _ = self._parse_run(['--print-overlays'])
        assert ns['run.print_overlays'] is True

    def test_x11(self):
        ns, _ = self._parse_run(['--x11'])
        assert ns['run.x11'] is True

    def test_podman_remote(self):
        ns, _ = self._parse_run(['--podman-remote'])
        assert ns['run.podman_remote'] is True

    def test_shell(self):
        ns, _ = self._parse_run(['--shell', '/bin/zsh'])
        assert ns['run.shell'] == '/bin/zsh'

    def test_login(self):
        ns, _ = self._parse_run(['--login'])
        assert ns['run.login'] is True

    def test_no_login(self):
        ns, _ = self._parse_run(['--no-login'])
        assert ns['run.login'] is False

    def test_login_default_none(self):
        ns, _ = self._parse_run([])
        assert ns['run.login'] is None

    def test_prompt_banner(self):
        ns, _ = self._parse_run(['--prompt-banner', 'My Banner'])
        assert ns['run.prompt_banner'] == 'My Banner'

    def test_auto_attach(self):
        ns, _ = self._parse_run(['--auto-attach'])
        assert ns['run.auto_attach'] is True

    def test_auto_replace(self):
        ns, _ = self._parse_run(['--auto-replace'])
        assert ns['run.auto_replace'] is True

    def test_export_append(self):
        ns, _ = self._parse_run(['--export', '/a:/b', '--export', '/c:/d:0'])
        assert ns['run.export'] == ['/a:/b', '/c:/d:0']

    def test_fuse_overlayfs(self):
        ns, _ = self._parse_run(['--fuse-overlayfs'])
        assert ns['run.fuse_overlayfs'] is True

    def test_fuse_overlayfs_default_none(self):
        ns, _ = self._parse_run([])
        assert ns['run.fuse_overlayfs'] is None

    def test_label_single(self):
        ns, _ = self._parse_run(['--label', 'app=test'])
        assert ns['run.label'] == ['app=test']

    def test_label_multiple(self):
        ns, _ = self._parse_run(['-l', 'app=test', '-l', 'env=dev'])
        assert ns['run.label'] == ['app=test', 'env=dev']

    def test_label_default_none(self):
        ns, _ = self._parse_run([])
        assert ns['run.label'] is None

    def test_podman_value_flags_collected(self):
        ns, _ = self._parse_run(['-e', 'FOO=bar', '-v', '/a:/b'])
        pt = ns.get('run.passthrough_args') or []
        assert '-e' in pt
        assert 'FOO=bar' in pt
        assert '-v' in pt
        assert '/a:/b' in pt

    def test_podman_value_flags_multiple(self):
        ns, _ = self._parse_run(['-e', 'A=1', '-e', 'B=2'])
        pt = ns.get('run.passthrough_args') or []
        assert pt == ['-e', 'A=1', '-e', 'B=2']

    def test_boolean_podman_flags_collected(self):
        ns, _ = self._parse_run(['--rm', '--privileged'])
        pt = ns.get('run.passthrough_args') or []
        assert '--rm' in pt
        assert '--privileged' in pt

    def test_defaults(self):
        ns, _ = self._parse_run([])
        assert ns['run.name'] is None
        assert ns['run.label'] is None
        assert ns['run.user_overlay'] is None
        assert ns['run.host_overlay'] is None
        assert ns['run.interactive_overlay'] is None
        assert ns['run.workspace'] is None
        assert ns['run.adhoc'] is None
        assert ns['run.print_overlays'] is False
        assert ns['run.x11'] is None
        assert ns['run.podman_remote'] is None
        assert ns['run.shell'] is None
        assert ns['run.login'] is None
        assert ns['run.prompt_banner'] is None
        assert ns['run.auto_attach'] is None
        assert ns['run.auto_replace'] is None
        assert ns['run.export'] is None
        assert ns['run.fuse_overlayfs'] is None

    def test_workspace_and_adhoc_together(self):
        ns, _ = self._parse_run(['--workspace', '--adhoc'])
        assert ns['run.workspace'] is True
        assert ns['run.adhoc'] is True

    def test_equals_syntax_for_podman_flags(self):
        ns, _ = self._parse_run(['--env=FOO=bar'])
        pt = ns.get('run.passthrough_args') or []
        assert '--env' in pt
        assert 'FOO=bar' in pt


# ---------------------------------------------------------------------------
# TestPassthroughAction
# ---------------------------------------------------------------------------


class TestPassthroughAction:
    def _parse_run(self, args):
        parser = build_root_parser()
        ns, unknowns = parser.parse_known_args(['run'] + args)
        return vars(ns), unknowns

    def test_value_flag_with_space(self):
        ns, _ = self._parse_run(['-e', 'FOO=bar'])
        pt = ns.get('run.passthrough_args') or []
        assert pt == ['-e', 'FOO=bar']

    def test_value_flag_with_equals(self):
        ns, _ = self._parse_run(['--env=FOO=bar'])
        pt = ns.get('run.passthrough_args') or []
        assert '--env' in pt
        assert 'FOO=bar' in pt

    def test_multiple_flags_accumulated_in_order(self):
        ns, _ = self._parse_run(['-e', 'FOO', '-e', 'BAR', '-v', '/a:/b'])
        pt = ns.get('run.passthrough_args') or []
        assert pt == ['-e', 'FOO', '-e', 'BAR', '-v', '/a:/b']

    def test_mount_flag(self):
        ns, _ = self._parse_run(['--mount', 'type=bind,src=/a,dst=/b'])
        pt = ns.get('run.passthrough_args') or []
        assert '--mount' in pt
        assert 'type=bind,src=/a,dst=/b' in pt

    def test_boolean_passthrough_on_root(self):
        """_PassthroughAction with nargs=0 captures boolean flags."""
        parser = build_root_parser()
        ns = vars(parser.parse_known_args(['--remote'])[0])
        assert ns.get('podman_global_args') or [] == ['--remote']



# ---------------------------------------------------------------------------
# TestParseArgs (end-to-end)
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_explicit_run_with_overlays_and_podman_flags(self):
        r = parse_args(['run', '--host-overlay', '-e', 'FOO=bar', 'alpine'])
        assert r.ns['subcommand'] == 'run'
        assert r.ns['run.host_overlay'] is True
        pt = r.ns.get('run.passthrough_args') or []
        assert '-e' in pt
        assert 'FOO=bar' in pt
        assert 'alpine' in r.trailing_args

    def test_global_flags_combined_with_run(self):
        r = parse_args(['--print-cmd', 'run', '--workspace', 'alpine'])
        assert r.ns['root.print_cmd'] is True
        assert r.ns['subcommand'] == 'run'
        assert r.ns['run.workspace'] is True
        assert 'alpine' in r.trailing_args

    def test_podman_global_flags_extracted(self):
        r = parse_args(['--root', '/x', 'run', 'alpine'])
        pga = r.ns.get('podman_global_args') or []
        assert '--root' in pga
        assert '/x' in pga
        assert r.ns['subcommand'] == 'run'

    def test_explicit_command_after_separator(self):
        r = parse_args(['run', 'alpine', '--', 'bash', '-c', 'echo hi'])
        assert r.explicit_command == ['bash', '-c', 'echo hi']
        assert r.ns['subcommand'] == 'run'

    def test_store_subcommand(self):
        r = parse_args(['store', 'init', '--store-dir', '/my/store'])
        assert r.ns['subcommand'] == 'store'
        assert r.ns['store.action'] == 'init'
        assert r.ns['store.store_dir'] == '/my/store'

    def test_store_destroy(self):
        r = parse_args(['store', 'destroy', '--store-dir', '/my/store'])
        assert r.ns['subcommand'] == 'store'
        assert r.ns['store.action'] == 'destroy'

    def test_store_info(self):
        r = parse_args(['store', 'info'])
        assert r.ns['subcommand'] == 'store'
        assert r.ns['store.action'] == 'info'

    def test_passthrough_subcommand_ps(self):
        r = parse_args(['ps', '-a'])
        assert r.ns['subcommand'] == 'ps'
        assert r.subcmd_passthrough_args == ['-a']

    def test_passthrough_subcommand_inspect(self):
        r = parse_args(['inspect', 'abc123'])
        assert r.ns['subcommand'] == 'inspect'
        assert r.subcmd_passthrough_args == ['abc123']

    def test_boolean_podman_flags_in_passthrough(self):
        r = parse_args(['run', '--rm', '--privileged', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '--rm' in pt
        assert '--privileged' in pt

    def test_podman_global_flags_with_equals(self):
        r = parse_args(['--root=/x', 'run', 'alpine'])
        pga = r.ns.get('podman_global_args') or []
        assert '--root' in pga
        assert '/x' in pga

    def test_multiple_podman_value_flags(self):
        r = parse_args(['run', '-e', 'A=1', '-e', 'B=2', '-v', '/a:/b', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '-e' in pt
        assert 'A=1' in pt
        assert '-v' in pt
        assert '/a:/b' in pt
        assert 'alpine' in r.trailing_args

    def test_raw_argv_preserved(self):
        r = parse_args(['run', '--workspace', 'alpine'])
        assert r.raw_argv == ['run', '--workspace', 'alpine']

    def test_store_flag_before_run_subcommand(self):
        """--store VALUE before 'run' — root parser consumes --store, routes to run."""
        r = parse_args(['--store', '/my/store', 'run', 'alpine'])
        assert r.ns['root.store'] == '/my/store'
        assert r.ns['subcommand'] == 'run'
        assert 'alpine' in r.trailing_args
        # No translation at parse time — resolution happens in Phase 2
        pga = r.ns.get('podman_global_args') or []
        assert '--root' not in pga

    def test_store_flag_equals_before_subcommand(self):
        r = parse_args(['--store=/my/store', 'run', 'alpine'])
        assert r.ns['root.store'] == '/my/store'
        assert r.ns['subcommand'] == 'run'

    def test_config_flag_before_subcommand(self):
        """--config VALUE before subcommand."""
        r = parse_args(['--config', '/path/to/config.json', 'run', 'alpine'])
        assert r.ns['root.config'] == '/path/to/config.json'
        assert r.ns['subcommand'] == 'run'

    def test_remote_before_passthrough(self):
        """--remote before passthrough subcommand lands in podman_global_args."""
        r = parse_args(['--remote', 'ps', '-a'])
        assert r.ns['subcommand'] == 'ps'
        pga = r.ns.get('podman_global_args') or []
        assert '--remote' in pga
        assert '-a' in r.subcmd_passthrough_args


# ---------------------------------------------------------------------------
# TestHelp — uses real podman
# ---------------------------------------------------------------------------


class TestHelp:
    def test_run_help_shows_podman_and_podrun(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            print_help('run', ['--help'], 'podman')
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'podrun' in out
        assert 'Podrun:' in out

    def test_global_help(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            print_help(None, ['--help'], 'podman')
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'podrun' in out
        assert 'store' in out

    def test_help_after_separator_ignored(self):
        print_help('run', ['image', '--', '--help'], 'podman')

    def test_store_help_not_handled(self):
        print_help('store', ['--help'], 'podman')

    def test_other_subcmd_help_not_handled(self):
        print_help('ps', ['--help'], 'podman')

    def test_no_help_flag_returns(self):
        print_help('run', ['alpine'], 'podman')


# ---------------------------------------------------------------------------
# TestPrintCmd
# ---------------------------------------------------------------------------


class TestPrintCmd:
    def test_run_print_cmd(self):
        r = parse_args(['--print-cmd', 'run', '-e', 'FOO=bar', 'alpine'])
        cmd = build_run_command(r, 'podman')
        assert cmd[0] == 'podman'
        assert 'run' in cmd
        assert '-e' in cmd
        assert 'FOO=bar' in cmd
        assert 'alpine' in cmd

    def test_run_print_cmd_global_flags_before_subcommand(self):
        r = parse_args(['--root', '/x', 'run', 'alpine'])
        cmd = build_run_command(r, 'podman')
        run_idx = cmd.index('run')
        root_idx = cmd.index('--root')
        assert root_idx < run_idx

    def test_passthrough_flags_and_image_and_command(self):
        r = parse_args(
            ['run', '--rm', '-e', 'A=1', '--privileged', 'alpine', 'bash', '-c', 'echo hi']
        )
        cmd = build_run_command(r, 'podman')
        assert '--rm' in cmd
        assert '-e' in cmd
        assert 'A=1' in cmd
        assert '--privileged' in cmd
        assert 'alpine' in cmd
        assert 'bash' in cmd

    def test_explicit_command_with_separator(self):
        r = parse_args(['run', 'alpine', '--', 'bash', '-c', 'echo hi'])
        cmd = build_run_command(r, 'podman')
        sep_idx = cmd.index('--')
        assert cmd[sep_idx + 1 :] == ['bash', '-c', 'echo hi']

    def test_store_print_cmd(self):
        r = parse_args(['store', 'init', '--store-dir', '/s'])
        cmd = build_store_command(r, 'podman')
        assert 'podman' in cmd
        assert 'store' in cmd
        assert 'init' in cmd

    def test_name_flag_in_run_command(self):
        r = parse_args(['run', '--name', 'mycontainer', 'alpine'])
        cmd = build_run_command(r, 'podman')
        assert '--name=mycontainer' in cmd


# ---------------------------------------------------------------------------
# TestBuildPassthroughCommand
# ---------------------------------------------------------------------------


class TestBuildPassthroughCommand:
    def test_simple_passthrough(self):
        r = parse_args(['ps', '-a'])
        cmd = build_passthrough_command(r, 'podman')
        assert cmd == ['podman', 'ps', '-a']

    def test_passthrough_with_global_flags(self):
        r = parse_args(['--root', '/x', 'ps', '-a'])
        cmd = build_passthrough_command(r, 'podman')
        assert cmd[0] == 'podman'
        ps_idx = cmd.index('ps')
        root_idx = cmd.index('--root')
        assert root_idx < ps_idx

    def test_passthrough_with_remote(self):
        r = parse_args(['--remote', 'ps', '-a'])
        cmd = build_passthrough_command(r, 'podman')
        remote_idx = cmd.index('--remote')
        ps_idx = cmd.index('ps')
        assert remote_idx < ps_idx

    def test_passthrough_with_explicit_command(self):
        r = parse_args(['exec', 'mycontainer', '--', 'bash'])
        cmd = build_passthrough_command(r, 'podman')
        assert 'exec' in cmd
        assert 'mycontainer' in cmd
        assert '--' in cmd
        assert 'bash' in cmd


# ---------------------------------------------------------------------------
# TestCompletion (stub)
# ---------------------------------------------------------------------------


class TestCompletion:
    def test_completion_bash_exits(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            print_completion('bash')
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'bash' in out

    def test_completion_zsh_exits(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            print_completion('zsh')
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'zsh' in out

    def test_completion_fish_exits(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            print_completion('fish')
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'fish' in out



# ---------------------------------------------------------------------------
# TestScrapePodmanHelp
# ---------------------------------------------------------------------------


class TestScrapePodmanHelp:
    def test_scrape_run_value_flags(self):
        result = _scrape_podman_help('podman', subcmd='run')
        assert result is not None
        value_flags, bool_flags, _ = result
        assert '--env' in value_flags
        assert '-e' in value_flags
        assert '--volume' in value_flags
        assert '--name' in value_flags
        assert '--rm' in bool_flags

    def test_scrape_global(self):
        result = _scrape_podman_help('podman')
        assert result is not None
        value_flags, bool_flags, subcommands = result
        assert '--log-level' in value_flags
        assert 'run' in subcommands
        assert 'ps' in subcommands
        assert 'exec' in subcommands

    def test_scrape_failure(self, mock_run_os_cmd):
        mock_run_os_cmd.set_return(returncode=1)
        assert _scrape_podman_help('podman') is None


# ---------------------------------------------------------------------------
# TestVersion
# ---------------------------------------------------------------------------


class TestVersion:
    def test_print_version_real(self, capsys):
        print_version('podman')
        out = capsys.readouterr().out
        assert 'podman version' in out
        assert 'podrun version' in out

    def test_print_version_podman_failure(self, mock_run_os_cmd, capsys):
        mock_run_os_cmd.set_return(returncode=1)
        print_version('podman')
        out = capsys.readouterr().out
        assert 'podrun version' in out


# ---------------------------------------------------------------------------
# TestMain
# ---------------------------------------------------------------------------


class TestMain:
    def test_version_flag_exits(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(['--version'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'podrun version' in out
        assert 'podman version' in out

    def test_version_short_flag(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(['-v'])
        assert exc_info.value.code == 0

    def test_completion_exits(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(['--completion', 'bash'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'bash' in out

    def test_print_cmd_run(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(['--print-cmd', 'run', 'alpine'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'run' in out
        assert 'alpine' in out

    def test_print_cmd_store(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(['--print-cmd', 'store', 'init'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'store' in out

    def test_print_cmd_passthrough(self, monkeypatch, capsys):
        monkeypatch.setattr(podrun2_mod.os, 'execvpe', lambda *a: None)
        with pytest.raises(SystemExit) as exc_info:
            main(['--print-cmd', 'ps', '-a'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'ps' in out
        assert '-a' in out

    def test_passthrough_subcommand_execs(self, monkeypatch):
        calls = []

        def fake_execvpe(*a):
            calls.append(a)
            raise SystemExit(0)

        monkeypatch.setattr(podrun2_mod.os, 'execvpe', fake_execvpe)
        with pytest.raises(SystemExit):
            main(['ps', '-a'])
        assert len(calls) == 1
        assert 'ps' in calls[0][1]
        assert '-a' in calls[0][1]

    def test_help_run(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(['run', '--help'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'Podrun:' in out

    def test_help_global(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(['--help'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'store' in out

    def test_print_cmd_with_global_podman_flags(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(['--print-cmd', '--root', '/x', 'run', 'alpine'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert out.index('--root') < out.index('run')

    def test_print_cmd_with_env_and_volume(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(['--print-cmd', 'run', '-e', 'A=1', '-e', 'B=2', '-v', '/a:/b', 'alpine'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert '-e' in out
        assert 'A=1' in out
        assert 'B=2' in out
        assert '-v' in out
        assert '/a:/b' in out
        assert 'alpine' in out

    def test_print_cmd_passthrough_with_remote(self, monkeypatch, capsys):
        """--remote should appear before the subcommand in passthrough output."""
        monkeypatch.setattr(podrun2_mod.os, 'execvpe', lambda *a: None)
        with pytest.raises(SystemExit) as exc_info:
            main(['--print-cmd', '--remote', 'ps', '-a'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert out.index('--remote') < out.index('ps')

    def test_print_cmd_with_store_no_translation(self, capsys):
        """--store is config only at parse time; no --root in printed command."""
        with pytest.raises(SystemExit) as exc_info:
            main(['--print-cmd', '--store', '/my/store', 'run', 'alpine'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert '--root' not in out
        assert 'run' in out
        assert 'alpine' in out


# ---------------------------------------------------------------------------
# TestPrintCmdOutput — verify printed command structure through main()
# ---------------------------------------------------------------------------


class TestPrintCmdOutput:
    """End-to-end --print-cmd tests that verify the full printed command.

    Each test calls main() and parses the output line into a token list
    via shlex.split, then asserts on ordering, presence, and structure.
    """

    def _cmd(self, argv, capsys, monkeypatch=None):
        """Run main(['--print-cmd'] + argv) and return the printed tokens.

        The first token (podman path) is normalized to 'podman' so tests
        don't depend on the absolute path returned by shutil.which.
        """
        import shlex as _shlex

        if monkeypatch:
            monkeypatch.setattr(podrun2_mod.os, 'execvpe', lambda *a: None)
        with pytest.raises(SystemExit) as exc_info:
            main(['--print-cmd'] + argv)
        assert exc_info.value.code == 0
        tokens = _shlex.split(capsys.readouterr().out)
        if tokens and tokens[0].endswith('podman'):
            tokens[0] = 'podman'
        return tokens

    # -- basic run -----------------------------------------------------------

    def test_bare_run(self, capsys):
        cmd = self._cmd(['run', 'alpine'], capsys)
        assert cmd == ['podman', 'run', 'alpine']

    def test_run_with_image_and_command(self, capsys):
        cmd = self._cmd(['run', 'alpine', 'bash', '-c', 'echo hi'], capsys)
        assert cmd == ['podman', 'run', 'alpine', 'bash', '-c', 'echo hi']

    def test_run_with_separator(self, capsys):
        cmd = self._cmd(['run', 'alpine', '--', 'bash', '-c', 'echo hi'], capsys)
        assert cmd == ['podman', 'run', 'alpine', '--', 'bash', '-c', 'echo hi']

    # -- multiple -e flags ---------------------------------------------------

    def test_multiple_env(self, capsys):
        cmd = self._cmd(['run', '-e', 'A=1', '-e', 'B=2', '-e', 'C=3', 'alpine'], capsys)
        assert cmd == ['podman', 'run', '-e', 'A=1', '-e', 'B=2', '-e', 'C=3', 'alpine']

    def test_env_equals_syntax(self, capsys):
        cmd = self._cmd(['run', '--env=FOO=bar', 'alpine'], capsys)
        assert '--env' in cmd
        assert 'FOO=bar' in cmd
        assert 'alpine' in cmd

    # -- multiple -v flags ---------------------------------------------------

    def test_multiple_volume(self, capsys):
        cmd = self._cmd(
            ['run', '-v', '/a:/b', '-v', '/c:/d', '-v', '/e:/f:ro', 'alpine'], capsys,
        )
        assert cmd == [
            'podman', 'run',
            '-v', '/a:/b', '-v', '/c:/d', '-v', '/e:/f:ro',
            'alpine',
        ]

    def test_volume_long_form(self, capsys):
        cmd = self._cmd(
            ['run', '--volume', '/a:/b', '--volume', '/c:/d', 'alpine'], capsys,
        )
        assert cmd == ['podman', 'run', '--volume', '/a:/b', '--volume', '/c:/d', 'alpine']

    # -- mixed env + volume + boolean flags ----------------------------------

    def test_env_volume_rm_privileged(self, capsys):
        cmd = self._cmd([
            'run', '--rm', '--privileged',
            '-e', 'A=1', '-e', 'B=2',
            '-v', '/host:/ctr', '-v', '/x:/y:ro',
            'alpine',
        ], capsys)
        run_idx = cmd.index('run')
        alpine_idx = cmd.index('alpine')
        # All flags between 'run' and image
        flags_section = cmd[run_idx + 1 : alpine_idx]
        assert '--rm' in flags_section
        assert '--privileged' in flags_section
        assert flags_section.count('-e') == 2
        assert flags_section.count('-v') == 2
        assert 'A=1' in flags_section
        assert 'B=2' in flags_section
        assert '/host:/ctr' in flags_section
        assert '/x:/y:ro' in flags_section

    # -- --name --------------------------------------------------------------

    def test_name_in_output(self, capsys):
        cmd = self._cmd(['run', '--name', 'myc', 'alpine'], capsys)
        assert '--name=myc' in cmd
        assert cmd.index('--name=myc') > cmd.index('run')
        assert cmd.index('--name=myc') < cmd.index('alpine')

    def test_name_with_passthrough_flags(self, capsys):
        cmd = self._cmd([
            'run', '--name', 'myc', '--rm', '-e', 'A=1', 'alpine',
        ], capsys)
        assert '--name=myc' in cmd
        assert '--rm' in cmd
        assert '-e' in cmd
        assert 'A=1' in cmd

    # -- global podman flags ordering ----------------------------------------

    def test_global_flags_before_run(self, capsys):
        cmd = self._cmd(['--root', '/x', '--log-level', 'debug', 'run', 'alpine'], capsys)
        run_idx = cmd.index('run')
        assert cmd.index('--root') < run_idx
        assert cmd.index('/x') < run_idx
        assert cmd.index('--log-level') < run_idx
        assert cmd.index('debug') < run_idx

    def test_remote_before_run(self, capsys):
        cmd = self._cmd(['--remote', 'run', 'alpine'], capsys)
        assert cmd.index('--remote') < cmd.index('run')

    def test_multiple_global_flags(self, capsys):
        cmd = self._cmd([
            '--root', '/x', '--log-level', 'debug', '--remote', 'run', 'alpine',
        ], capsys)
        run_idx = cmd.index('run')
        for tok in ('--root', '/x', '--log-level', 'debug', '--remote'):
            assert cmd.index(tok) < run_idx

    # -- global flags + passthrough subcommand -------------------------------

    def test_passthrough_ps(self, capsys, monkeypatch):
        cmd = self._cmd(['ps', '-a'], capsys, monkeypatch)
        assert cmd == ['podman', 'ps', '-a']

    def test_passthrough_with_global(self, capsys, monkeypatch):
        cmd = self._cmd(['--remote', 'ps', '-a', '--format', 'json'], capsys, monkeypatch)
        assert cmd.index('--remote') < cmd.index('ps')
        assert '-a' in cmd
        assert '--format' in cmd
        assert 'json' in cmd

    # -- image boundary (REMAINDER) ------------------------------------------

    def test_command_flag_not_consumed(self, capsys):
        """'-c' after image is part of the command, not consumed as --cpu-shares."""
        cmd = self._cmd(['run', '-e', 'A=1', 'alpine', 'bash', '-c', 'echo hi'], capsys)
        assert cmd == ['podman', 'run', '-e', 'A=1', 'alpine', 'bash', '-c', 'echo hi']

    def test_flag_like_command_args(self, capsys):
        """All flag-like tokens after image are passed through literally."""
        cmd = self._cmd(['run', 'alpine', 'ls', '-la', '--color=auto'], capsys)
        assert cmd == ['podman', 'run', 'alpine', 'ls', '-la', '--color=auto']

    def test_double_dash_command_with_flags(self, capsys):
        """Explicit '--' separator still works."""
        cmd = self._cmd(['run', '-e', 'A=1', 'alpine', '--', 'bash', '-c', 'echo'], capsys)
        sep_idx = cmd.index('--')
        assert cmd[sep_idx + 1:] == ['bash', '-c', 'echo']

    # -- rich combinations ---------------------------------------------------

    def test_global_plus_run_flags_plus_env_volume(self, capsys):
        cmd = self._cmd([
            '--root', '/x',
            'run', '--name', 'dev', '--rm',
            '-e', 'FOO=bar', '-e', 'BAZ=qux',
            '-v', '/a:/b', '-v', '/c:/d',
            'ubuntu:22.04',
        ], capsys)
        run_idx = cmd.index('run')
        img_idx = cmd.index('ubuntu:22.04')
        # Global before run
        assert cmd.index('--root') < run_idx
        # Everything between run and image
        mid = cmd[run_idx + 1 : img_idx]
        assert '--name=dev' in mid
        assert '--rm' in mid
        assert mid.count('-e') == 2
        assert mid.count('-v') == 2

    def test_full_realistic_command(self, capsys):
        """Realistic workspace-like invocation."""
        cmd = self._cmd([
            '--root', '/x', '--log-level', 'debug',
            'run', '--name', 'workspace',
            '--rm', '--privileged',
            '-e', 'DISPLAY=:0', '-e', 'HOME=/home/user',
            '-v', '/home/user:/home/user',
            '-v', '/tmp/.X11-unix:/tmp/.X11-unix',
            '--network', 'host',
            '-w', '/home/user/project',
            'dev-image:latest', 'bash',
        ], capsys)
        run_idx = cmd.index('run')
        img_idx = cmd.index('dev-image:latest')
        # Global flags before run
        assert cmd.index('--root') < run_idx
        assert cmd.index('--log-level') < run_idx
        # --name after run
        assert '--name=workspace' in cmd
        # Boolean flags present
        assert '--rm' in cmd
        assert '--privileged' in cmd
        # Multiple env vars
        assert cmd.count('-e') == 2
        assert 'DISPLAY=:0' in cmd
        assert 'HOME=/home/user' in cmd
        # Multiple volumes
        assert cmd.count('-v') == 2
        assert '/home/user:/home/user' in cmd
        assert '/tmp/.X11-unix:/tmp/.X11-unix' in cmd
        # Other value flags
        assert '--network' in cmd
        assert 'host' in cmd
        assert '-w' in cmd
        assert '/home/user/project' in cmd
        # Image + command at end
        assert img_idx > run_idx
        assert cmd[img_idx + 1] == 'bash'

    def test_full_with_command_and_separator(self, capsys):
        cmd = self._cmd([
            '--root', '/x',
            'run', '--name', 'myc', '--rm',
            '-e', 'A=1', '-v', '/a:/b',
            'alpine', '--', 'sh', '-c', 'echo done',
        ], capsys)
        run_idx = cmd.index('run')
        sep_idx = cmd.index('--')
        img_idx = cmd.index('alpine')
        assert cmd.index('--root') < run_idx
        assert '--name=myc' in cmd
        assert run_idx < img_idx < sep_idx
        assert cmd[sep_idx + 1:] == ['sh', '-c', 'echo done']

    def test_mount_flags(self, capsys):
        cmd = self._cmd([
            'run',
            '--mount', 'type=bind,src=/a,dst=/b',
            '--mount', 'type=tmpfs,dst=/tmp',
            'alpine',
        ], capsys)
        assert cmd.count('--mount') == 2
        assert 'type=bind,src=/a,dst=/b' in cmd
        assert 'type=tmpfs,dst=/tmp' in cmd

    def test_label_and_annotation(self, capsys):
        cmd = self._cmd([
            'run',
            '-l', 'app=test', '-l', 'env=dev',
            '--annotation', 'note=hello',
            'alpine',
        ], capsys)
        assert '--label=app=test' in cmd
        assert '--label=env=dev' in cmd
        assert '--annotation' in cmd
        assert 'note=hello' in cmd

    def test_memory_cpus_user(self, capsys):
        cmd = self._cmd([
            'run', '-m', '512m', '--cpus', '2', '-u', '1000:1000', 'alpine',
        ], capsys)
        assert '-m' in cmd
        assert '512m' in cmd
        assert '--cpus' in cmd
        assert '2' in cmd
        assert '-u' in cmd
        assert '1000:1000' in cmd

    def test_publish_and_network(self, capsys):
        cmd = self._cmd([
            'run', '-p', '8080:80', '-p', '443:443', '--network', 'bridge', 'nginx',
        ], capsys)
        assert cmd.count('-p') == 2
        assert '8080:80' in cmd
        assert '443:443' in cmd
        assert '--network' in cmd
        assert 'bridge' in cmd
        assert 'nginx' in cmd

    def test_store_init_full(self, capsys):
        cmd = self._cmd([
            'store', 'init', '--store-dir', '/my/store',
            '--registry', 'mirror.io', '--storage-driver', 'vfs',
        ], capsys)
        assert 'store' in cmd
        assert 'init' in cmd
        assert '--store-dir' in cmd
        assert '/my/store' in cmd
        assert '--registry' in cmd
        assert 'mirror.io' in cmd
        assert '--storage-driver' in cmd
        assert 'vfs' in cmd


# ---------------------------------------------------------------------------
# TestRunOsCmd — real subprocess
# ---------------------------------------------------------------------------


class TestRunOsCmd:
    def test_echo(self):
        result = run_os_cmd('echo hello')
        assert result.returncode == 0
        assert 'hello' in result.stdout

    def test_false(self):
        result = run_os_cmd('false')
        assert result.returncode != 0

    def test_podman_version(self):
        result = run_os_cmd('podman --version')
        assert result.returncode == 0
        assert 'podman' in result.stdout


# ---------------------------------------------------------------------------
# TestBuildStoreParser — test store subparser through parse_args
# ---------------------------------------------------------------------------


class TestBuildStoreParser:
    def test_init_subcommand(self):
        r = parse_args(['store', 'init', '--store-dir', '/s', '--registry', 'r'])
        assert r.ns['store.action'] == 'init'
        assert r.ns['store.store_dir'] == '/s'
        assert r.ns['store.registry'] == 'r'

    def test_init_defaults(self):
        r = parse_args(['store', 'init'])
        assert r.ns['store.action'] == 'init'
        assert r.ns['store.store_dir'] == '.devcontainer/.podrun/store'
        assert r.ns['store.registry'] is None
        assert r.ns['store.storage_driver'] == 'overlay'

    def test_destroy_subcommand(self):
        r = parse_args(['store', 'destroy', '--store-dir', '/s'])
        assert r.ns['store.action'] == 'destroy'

    def test_info_subcommand(self):
        r = parse_args(['store', 'info', '--store-dir', '/s'])
        assert r.ns['store.action'] == 'info'

    def test_no_action(self):
        r = parse_args(['store'])
        assert r.ns['store.action'] is None


# ---------------------------------------------------------------------------
# TestBuildRunCommand
# ---------------------------------------------------------------------------


class TestBuildRunCommand:
    def test_basic_run(self):
        r = parse_args(['run', 'alpine'])
        cmd = build_run_command(r, 'podman')
        assert cmd == ['podman', 'run', 'alpine']

    def test_with_global_flags(self):
        r = parse_args(['--root', '/x', 'run', 'alpine'])
        cmd = build_run_command(r, 'podman')
        assert cmd.index('--root') < cmd.index('run')
        assert cmd.index('/x') < cmd.index('run')

    def test_with_passthrough_flags(self):
        r = parse_args(['run', '-e', 'A=1', '--rm', 'alpine'])
        cmd = build_run_command(r, 'podman')
        assert '-e' in cmd
        assert 'A=1' in cmd
        assert '--rm' in cmd
        assert 'alpine' in cmd

    def test_with_explicit_command(self):
        r = parse_args(['run', 'alpine', '--', 'echo', 'hi'])
        cmd = build_run_command(r, 'podman')
        assert '--' in cmd
        assert cmd[cmd.index('--') + 1 :] == ['echo', 'hi']

    def test_with_name(self):
        r = parse_args(['run', '--name', 'myc', 'alpine'])
        cmd = build_run_command(r, 'podman')
        assert '--name=myc' in cmd

    def test_store_not_in_run_command(self):
        """--store is config only; no --root injected at parse time."""
        r = parse_args(['--store', '/s', 'run', 'alpine'])
        cmd = build_run_command(r, 'podman')
        assert '--root' not in cmd
        assert cmd == ['podman', 'run', 'alpine']


# ---------------------------------------------------------------------------
# TestBuildStoreCommand
# ---------------------------------------------------------------------------


class TestBuildStoreCommand:
    def test_init_command(self):
        r = parse_args(['store', 'init', '--store-dir', '/s', '--registry', 'r'])
        cmd = build_store_command(r, 'podman')
        assert 'podman' in cmd
        assert 'store' in cmd
        assert 'init' in cmd
        assert '--store-dir' in cmd
        assert '/s' in cmd
        assert '--registry' in cmd
        assert 'r' in cmd

    def test_destroy_command(self):
        r = parse_args(['store', 'destroy'])
        cmd = build_store_command(r, 'podman')
        assert 'destroy' in cmd

    def test_info_command(self):
        r = parse_args(['store', 'info'])
        cmd = build_store_command(r, 'podman')
        assert 'info' in cmd


# ---------------------------------------------------------------------------
# TestRootFlagCombinations — multiple root flags together
# ---------------------------------------------------------------------------


class TestRootFlagCombinations:
    def test_print_cmd_and_config(self):
        r = parse_args(['--print-cmd', '--config', '/c.json', 'run', 'alpine'])
        assert r.ns['root.print_cmd'] is True
        assert r.ns['root.config'] == '/c.json'

    def test_print_cmd_and_no_devconfig(self):
        r = parse_args(['--print-cmd', '--no-devconfig', 'run', 'alpine'])
        assert r.ns['root.print_cmd'] is True
        assert r.ns['root.no_devconfig'] is True

    def test_config_and_no_devconfig(self):
        r = parse_args(['--config', '/c.json', '--no-devconfig', 'run', 'alpine'])
        assert r.ns['root.config'] == '/c.json'
        assert r.ns['root.no_devconfig'] is True

    def test_config_and_config_script(self):
        r = parse_args(['--config', '/c.json', '--config-script', '/s.sh', 'run', 'alpine'])
        assert r.ns['root.config'] == '/c.json'
        assert r.ns['root.config_script'] == ['/s.sh']

    def test_store_and_auto_init_and_registry(self):
        r = parse_args([
            '--store', '/s', '--auto-init-store', '--store-registry', 'mirror.io', 'run', 'alpine',
        ])
        assert r.ns['root.store'] == '/s'
        assert r.ns['root.auto_init_store'] is True
        assert r.ns['root.store.registry'] == 'mirror.io'

    def test_store_and_ignore_store(self):
        """Both flags parse — resolution conflict is handled in Phase 2."""
        r = parse_args(['--store', '/s', '--ignore-store', 'run', 'alpine'])
        assert r.ns['root.store'] == '/s'
        assert r.ns['root.ignore_store'] is True

    def test_all_root_flags_together(self):
        r = parse_args([
            '--print-cmd', '--config', '/c.json', '--config-script', '/s.sh',
            '--no-devconfig', '--store', '/s', '--ignore-store',
            '--auto-init-store', '--store-registry', 'r.io',
            'run', 'alpine',
        ])
        assert r.ns['root.print_cmd'] is True
        assert r.ns['root.config'] == '/c.json'
        assert r.ns['root.config_script'] == ['/s.sh']
        assert r.ns['root.no_devconfig'] is True
        assert r.ns['root.store'] == '/s'
        assert r.ns['root.ignore_store'] is True
        assert r.ns['root.auto_init_store'] is True
        assert r.ns['root.store.registry'] == 'r.io'
        assert r.ns['subcommand'] == 'run'


# ---------------------------------------------------------------------------
# TestRootAndRunCombinations — root flags + run flags together
# ---------------------------------------------------------------------------


class TestRootAndRunCombinations:
    def test_config_before_run_with_overlay(self):
        r = parse_args(['--config', '/c.json', 'run', '--host-overlay', 'alpine'])
        assert r.ns['root.config'] == '/c.json'
        assert r.ns['run.host_overlay'] is True
        assert 'alpine' in r.trailing_args

    def test_no_devconfig_before_run_with_workspace(self):
        r = parse_args(['--no-devconfig', 'run', '--workspace', 'alpine'])
        assert r.ns['root.no_devconfig'] is True
        assert r.ns['run.workspace'] is True

    def test_store_before_run_with_name_and_adhoc(self):
        r = parse_args([
            '--store', '/s', 'run', '--name', 'myc', '--adhoc', 'alpine',
        ])
        assert r.ns['root.store'] == '/s'
        assert r.ns['run.name'] == 'myc'
        assert r.ns['run.adhoc'] is True

    def test_ignore_store_before_run_with_shell(self):
        r = parse_args(['--ignore-store', 'run', '--shell', '/bin/zsh', 'alpine'])
        assert r.ns['root.ignore_store'] is True
        assert r.ns['run.shell'] == '/bin/zsh'

    def test_config_script_before_run_with_export(self):
        r = parse_args([
            '--config-script', '/s.sh', 'run', '--export', '/a:/b', 'alpine',
        ])
        assert r.ns['root.config_script'] == ['/s.sh']
        assert r.ns['run.export'] == ['/a:/b']

    def test_print_cmd_with_multiple_run_flags(self):
        r = parse_args([
            '--print-cmd', 'run', '--workspace', '--name', 'myc',
            '--shell', '/bin/zsh', '--login', 'alpine',
        ])
        assert r.ns['root.print_cmd'] is True
        assert r.ns['run.workspace'] is True
        assert r.ns['run.name'] == 'myc'
        assert r.ns['run.shell'] == '/bin/zsh'
        assert r.ns['run.login'] is True

    def test_multiple_root_flags_with_multiple_run_flags(self):
        r = parse_args([
            '--print-cmd', '--config', '/c.json', '--no-devconfig',
            '--store', '/s', '--auto-init-store',
            'run', '--host-overlay', '--name', 'myc', '--x11',
            '--export', '/a:/b', '--export', '/c:/d',
            'alpine',
        ])
        assert r.ns['root.print_cmd'] is True
        assert r.ns['root.config'] == '/c.json'
        assert r.ns['root.no_devconfig'] is True
        assert r.ns['root.store'] == '/s'
        assert r.ns['root.auto_init_store'] is True
        assert r.ns['run.host_overlay'] is True
        assert r.ns['run.name'] == 'myc'
        assert r.ns['run.x11'] is True
        assert r.ns['run.export'] == ['/a:/b', '/c:/d']
        assert 'alpine' in r.trailing_args


# ---------------------------------------------------------------------------
# TestRunFlagCombinations — multiple run flags together
# ---------------------------------------------------------------------------


class TestRunFlagCombinations:
    def test_overlay_hierarchy_user_and_host(self):
        r = parse_args(['run', '--user-overlay', '--host-overlay', 'alpine'])
        assert r.ns['run.user_overlay'] is True
        assert r.ns['run.host_overlay'] is True

    def test_overlay_hierarchy_host_and_interactive(self):
        r = parse_args(['run', '--host-overlay', '--interactive-overlay', 'alpine'])
        assert r.ns['run.host_overlay'] is True
        assert r.ns['run.interactive_overlay'] is True

    def test_overlay_hierarchy_all(self):
        r = parse_args([
            'run', '--user-overlay', '--host-overlay', '--interactive-overlay',
            '--workspace', '--adhoc', 'alpine',
        ])
        assert r.ns['run.user_overlay'] is True
        assert r.ns['run.host_overlay'] is True
        assert r.ns['run.interactive_overlay'] is True
        assert r.ns['run.workspace'] is True
        assert r.ns['run.adhoc'] is True

    def test_workspace_with_name_and_shell(self):
        r = parse_args(['run', '--workspace', '--name', 'myc', '--shell', '/bin/zsh', 'alpine'])
        assert r.ns['run.workspace'] is True
        assert r.ns['run.name'] == 'myc'
        assert r.ns['run.shell'] == '/bin/zsh'

    def test_adhoc_with_shell_and_login(self):
        r = parse_args(['run', '--adhoc', '--shell', '/bin/bash', '--login', 'alpine'])
        assert r.ns['run.adhoc'] is True
        assert r.ns['run.shell'] == '/bin/bash'
        assert r.ns['run.login'] is True

    def test_adhoc_with_shell_and_no_login(self):
        r = parse_args(['run', '--adhoc', '--shell', '/bin/bash', '--no-login', 'alpine'])
        assert r.ns['run.adhoc'] is True
        assert r.ns['run.shell'] == '/bin/bash'
        assert r.ns['run.login'] is False

    def test_adhoc_with_name_and_x11(self):
        r = parse_args(['run', '--adhoc', '--name', 'x11c', '--x11', 'alpine'])
        assert r.ns['run.adhoc'] is True
        assert r.ns['run.name'] == 'x11c'
        assert r.ns['run.x11'] is True

    def test_name_with_auto_attach(self):
        r = parse_args(['run', '--name', 'myc', '--auto-attach', 'alpine'])
        assert r.ns['run.name'] == 'myc'
        assert r.ns['run.auto_attach'] is True

    def test_name_with_auto_replace(self):
        r = parse_args(['run', '--name', 'myc', '--auto-replace', 'alpine'])
        assert r.ns['run.name'] == 'myc'
        assert r.ns['run.auto_replace'] is True

    def test_fuse_overlayfs_with_overlay_flags(self):
        r = parse_args([
            'run', '--fuse-overlayfs', '--host-overlay', '--interactive-overlay', 'alpine',
        ])
        assert r.ns['run.fuse_overlayfs'] is True
        assert r.ns['run.host_overlay'] is True
        assert r.ns['run.interactive_overlay'] is True

    def test_export_with_workspace_and_name(self):
        r = parse_args([
            'run', '--workspace', '--name', 'myc',
            '--export', '/src:/dst', '--export', '/a:/b:0', 'alpine',
        ])
        assert r.ns['run.workspace'] is True
        assert r.ns['run.name'] == 'myc'
        assert r.ns['run.export'] == ['/src:/dst', '/a:/b:0']

    def test_podman_remote_with_workspace(self):
        r = parse_args(['run', '--podman-remote', '--workspace', 'alpine'])
        assert r.ns['run.podman_remote'] is True
        assert r.ns['run.workspace'] is True

    def test_prompt_banner_with_shell_and_login(self):
        r = parse_args([
            'run', '--prompt-banner', 'DEV', '--shell', '/bin/zsh', '--login', 'alpine',
        ])
        assert r.ns['run.prompt_banner'] == 'DEV'
        assert r.ns['run.shell'] == '/bin/zsh'
        assert r.ns['run.login'] is True

    def test_all_run_flags_together(self):
        r = parse_args([
            'run', '--name', 'full', '--user-overlay', '--host-overlay',
            '--interactive-overlay', '--workspace', '--adhoc',
            '--x11', '--podman-remote', '--shell', '/bin/zsh', '--login',
            '--prompt-banner', 'ALL', '--auto-attach', '--auto-replace',
            '--export', '/a:/b', '--fuse-overlayfs', '--print-overlays',
            'alpine',
        ])
        assert r.ns['run.name'] == 'full'
        assert r.ns['run.user_overlay'] is True
        assert r.ns['run.host_overlay'] is True
        assert r.ns['run.interactive_overlay'] is True
        assert r.ns['run.workspace'] is True
        assert r.ns['run.adhoc'] is True
        assert r.ns['run.x11'] is True
        assert r.ns['run.podman_remote'] is True
        assert r.ns['run.shell'] == '/bin/zsh'
        assert r.ns['run.login'] is True
        assert r.ns['run.prompt_banner'] == 'ALL'
        assert r.ns['run.auto_attach'] is True
        assert r.ns['run.auto_replace'] is True
        assert r.ns['run.export'] == ['/a:/b']
        assert r.ns['run.fuse_overlayfs'] is True
        assert r.ns['run.print_overlays'] is True
        assert 'alpine' in r.trailing_args


# ---------------------------------------------------------------------------
# TestPodmanPassthroughWithRunFlags — podman flags + podrun run flags
# ---------------------------------------------------------------------------


class TestPodmanPassthroughWithRunFlags:
    def test_env_and_volume_with_workspace(self):
        r = parse_args(['run', '--workspace', '-e', 'A=1', '-v', '/a:/b', 'alpine'])
        assert r.ns['run.workspace'] is True
        pt = r.ns.get('run.passthrough_args') or []
        assert '-e' in pt
        assert 'A=1' in pt
        assert '-v' in pt
        assert '/a:/b' in pt

    def test_rm_and_privileged_with_adhoc(self):
        r = parse_args(['run', '--adhoc', '--rm', '--privileged', 'alpine'])
        assert r.ns['run.adhoc'] is True
        pt = r.ns.get('run.passthrough_args') or []
        assert '--rm' in pt
        assert '--privileged' in pt

    def test_multiple_env_with_name_and_overlay(self):
        r = parse_args([
            'run', '--name', 'myc', '--host-overlay',
            '-e', 'A=1', '-e', 'B=2', '-e', 'C=3', 'alpine',
        ])
        assert r.ns['run.name'] == 'myc'
        assert r.ns['run.host_overlay'] is True
        pt = r.ns.get('run.passthrough_args') or []
        assert pt == ['-e', 'A=1', '-e', 'B=2', '-e', 'C=3']

    def test_mount_with_user_overlay(self):
        r = parse_args([
            'run', '--user-overlay', '--mount', 'type=bind,src=/a,dst=/b', 'alpine',
        ])
        assert r.ns['run.user_overlay'] is True
        pt = r.ns.get('run.passthrough_args') or []
        assert '--mount' in pt
        assert 'type=bind,src=/a,dst=/b' in pt

    def test_user_flag_with_shell(self):
        r = parse_args(['run', '--shell', '/bin/zsh', '-u', '1000:1000', 'alpine'])
        assert r.ns['run.shell'] == '/bin/zsh'
        pt = r.ns.get('run.passthrough_args') or []
        assert '-u' in pt
        assert '1000:1000' in pt

    def test_hostname_with_workspace(self):
        r = parse_args(['run', '--workspace', '-h', 'devbox', 'alpine'])
        assert r.ns['run.workspace'] is True
        pt = r.ns.get('run.passthrough_args') or []
        assert '-h' in pt
        assert 'devbox' in pt

    def test_network_with_x11(self):
        r = parse_args(['run', '--x11', '--network', 'host', 'alpine'])
        assert r.ns['run.x11'] is True
        pt = r.ns.get('run.passthrough_args') or []
        assert '--network' in pt
        assert 'host' in pt

    def test_boolean_and_value_podman_flags_with_all_overlays(self):
        r = parse_args([
            'run', '--workspace', '--fuse-overlayfs',
            '--rm', '--privileged', '-e', 'A=1', '-v', '/x:/y',
            '--network', 'host', '--hostname', 'dev',
            'alpine', 'bash',
        ])
        assert r.ns['run.workspace'] is True
        assert r.ns['run.fuse_overlayfs'] is True
        pt = r.ns.get('run.passthrough_args') or []
        assert '--rm' in pt
        assert '--privileged' in pt
        assert '-e' in pt
        assert 'A=1' in pt
        assert '-v' in pt
        assert '/x:/y' in pt
        assert '--network' in pt
        assert 'host' in pt
        assert '--hostname' in pt
        assert 'dev' in pt
        assert 'alpine' in r.trailing_args
        assert 'bash' in r.trailing_args

    def test_memory_and_cpus_with_name(self):
        r = parse_args([
            'run', '--name', 'limited', '-m', '512m', '--cpus', '2', 'alpine',
        ])
        assert r.ns['run.name'] == 'limited'
        pt = r.ns.get('run.passthrough_args') or []
        assert '-m' in pt
        assert '512m' in pt
        assert '--cpus' in pt
        assert '2' in pt

    def test_label_and_annotation_with_export(self):
        r = parse_args([
            'run', '--export', '/a:/b',
            '-l', 'app=test', '--annotation', 'key=val', 'alpine',
        ])
        assert r.ns['run.export'] == ['/a:/b']
        assert r.ns['run.label'] == ['app=test']
        pt = r.ns.get('run.passthrough_args') or []
        assert '--annotation' in pt
        assert 'key=val' in pt


# ---------------------------------------------------------------------------
# TestGlobalPodmanWithRunCombinations — podman global + run flags
# ---------------------------------------------------------------------------


class TestGlobalPodmanWithRunCombinations:
    def test_remote_with_run_and_workspace(self):
        r = parse_args(['--remote', 'run', '--workspace', 'alpine'])
        pga = r.ns.get('podman_global_args') or []
        assert '--remote' in pga
        assert r.ns['run.workspace'] is True

    def test_root_and_log_level_with_run_flags(self):
        r = parse_args([
            '--root', '/x', '--log-level', 'debug',
            'run', '--host-overlay', '--name', 'myc', 'alpine',
        ])
        pga = r.ns.get('podman_global_args') or []
        assert '--root' in pga
        assert '/x' in pga
        assert '--log-level' in pga
        assert 'debug' in pga
        assert r.ns['run.host_overlay'] is True
        assert r.ns['run.name'] == 'myc'

    def test_remote_with_run_podman_flags(self):
        r = parse_args(['--remote', 'run', '-e', 'A=1', '--rm', 'alpine'])
        pga = r.ns.get('podman_global_args') or []
        assert '--remote' in pga
        pt = r.ns.get('run.passthrough_args') or []
        assert '-e' in pt
        assert '--rm' in pt

    def test_storage_opt_with_run_and_name(self):
        r = parse_args([
            '--storage-opt', 'ignore_chown_errors=true',
            'run', '--name', 'myc', 'alpine',
        ])
        pga = r.ns.get('podman_global_args') or []
        assert '--storage-opt' in pga
        assert 'ignore_chown_errors=true' in pga
        assert r.ns['run.name'] == 'myc'

    def test_multiple_global_flags_with_run_podman_and_podrun_flags(self):
        r = parse_args([
            '--root', '/x', '--log-level', 'debug', '--remote',
            'run', '--workspace', '--name', 'dev',
            '-e', 'A=1', '-v', '/a:/b', '--rm',
            'alpine', 'bash',
        ])
        pga = r.ns.get('podman_global_args') or []
        assert '--root' in pga
        assert '--log-level' in pga
        assert '--remote' in pga
        assert r.ns['run.workspace'] is True
        assert r.ns['run.name'] == 'dev'
        pt = r.ns.get('run.passthrough_args') or []
        assert '-e' in pt
        assert '-v' in pt
        assert '--rm' in pt
        assert 'alpine' in r.trailing_args
        assert 'bash' in r.trailing_args

    def test_global_flags_with_passthrough_subcommand(self):
        r = parse_args(['--root', '/x', '--remote', 'ps', '-a', '--format', 'json'])
        pga = r.ns.get('podman_global_args') or []
        assert '--root' in pga
        assert '--remote' in pga
        assert r.ns['subcommand'] == 'ps'
        assert '-a' in r.subcmd_passthrough_args
        assert '--format' in r.subcmd_passthrough_args


# ---------------------------------------------------------------------------
# TestFullStackCombinations — root + global podman + run + podman run + image + cmd
# ---------------------------------------------------------------------------


class TestFullStackCombinations:
    def test_everything_together(self):
        """All layers: root flags + podman global + run podrun + run podman + image + cmd."""
        r = parse_args([
            '--print-cmd', '--config', '/c.json', '--no-devconfig',
            '--store', '/s', '--auto-init-store',
            '--root', '/x', '--log-level', 'debug',
            'run', '--workspace', '--name', 'full',
            '--shell', '/bin/zsh', '--login', '--x11',
            '--export', '/a:/b', '--fuse-overlayfs',
            '-e', 'FOO=bar', '-v', '/host:/ctr', '--rm', '--privileged',
            '--network', 'host',
            'myimage:latest', 'bash', '-c', 'echo hi',
        ])
        # Root flags
        assert r.ns['root.print_cmd'] is True
        assert r.ns['root.config'] == '/c.json'
        assert r.ns['root.no_devconfig'] is True
        assert r.ns['root.store'] == '/s'
        assert r.ns['root.auto_init_store'] is True
        # Podman global args
        pga = r.ns.get('podman_global_args') or []
        assert '--root' in pga
        assert '/x' in pga
        assert '--log-level' in pga
        assert 'debug' in pga
        # Podrun run flags
        assert r.ns['run.workspace'] is True
        assert r.ns['run.name'] == 'full'
        assert r.ns['run.shell'] == '/bin/zsh'
        assert r.ns['run.login'] is True
        assert r.ns['run.x11'] is True
        assert r.ns['run.export'] == ['/a:/b']
        assert r.ns['run.fuse_overlayfs'] is True
        # Podman run passthrough
        pt = r.ns.get('run.passthrough_args') or []
        assert '-e' in pt
        assert 'FOO=bar' in pt
        assert '-v' in pt
        assert '/host:/ctr' in pt
        assert '--rm' in pt
        assert '--privileged' in pt
        assert '--network' in pt
        assert 'host' in pt
        # Trailing (image + command — no '--' needed)
        assert r.trailing_args == ['myimage:latest', 'bash', '-c', 'echo hi']
        assert r.ns['subcommand'] == 'run'

    def test_everything_with_separator(self):
        """Full stack with explicit command after '--'."""
        r = parse_args([
            '--print-cmd', '--store', '/s',
            '--root', '/x',
            'run', '--adhoc', '--name', 'myc',
            '-e', 'A=1', '--rm',
            'alpine', '--', 'sh', '-c', 'echo done',
        ])
        assert r.ns['root.print_cmd'] is True
        assert r.ns['root.store'] == '/s'
        pga = r.ns.get('podman_global_args') or []
        assert '--root' in pga
        assert r.ns['run.adhoc'] is True
        assert r.ns['run.name'] == 'myc'
        pt = r.ns.get('run.passthrough_args') or []
        assert '-e' in pt
        assert '--rm' in pt
        assert 'alpine' in r.trailing_args
        assert r.explicit_command == ['sh', '-c', 'echo done']

    def test_build_run_command_full_stack(self):
        """build_run_command produces correct ordering with all flag types."""
        r = parse_args([
            '--root', '/x', '--log-level', 'debug',
            'run', '--name', 'myc',
            '-e', 'A=1', '-v', '/a:/b', '--rm',
            'alpine', '--', 'echo', 'hi',
        ])
        cmd = build_run_command(r, 'podman')
        # podman + global args before 'run'
        run_idx = cmd.index('run')
        assert cmd[0] == 'podman'
        assert cmd.index('--root') < run_idx
        assert cmd.index('/x') < run_idx
        assert cmd.index('--log-level') < run_idx
        # --name after 'run'
        assert '--name=myc' in cmd
        assert cmd.index('--name=myc') > run_idx
        # Passthrough flags after 'run'
        assert '-e' in cmd
        assert '--rm' in cmd
        # Image before separator
        assert 'alpine' in cmd
        # Explicit command after '--'
        sep_idx = cmd.index('--')
        assert cmd[sep_idx + 1:] == ['echo', 'hi']

    def test_build_run_command_boolean_and_value_flags(self):
        r = parse_args([
            'run', '--name', 'myc', '--rm', '--privileged',
            '-e', 'A=1', '-v', '/a:/b', '-m', '512m',
            'alpine',
        ])
        cmd = build_run_command(r, 'podman')
        assert '--name=myc' in cmd
        assert '--rm' in cmd
        assert '--privileged' in cmd
        assert '-e' in cmd
        assert 'A=1' in cmd
        assert '-v' in cmd
        assert '/a:/b' in cmd
        assert '-m' in cmd
        assert '512m' in cmd
        assert 'alpine' in cmd

    def test_print_cmd_main_full_stack(self, capsys):
        """main() with --print-cmd outputs correct full command."""
        with pytest.raises(SystemExit) as exc_info:
            main([
                '--print-cmd', '--root', '/x',
                'run', '--name', 'myc', '-e', 'A=1', '--rm',
                'alpine', '--', 'echo', 'hi',
            ])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert '--root' in out
        assert out.index('--root') < out.index('run')
        assert '--name=myc' in out
        assert '-e' in out
        assert '--rm' in out
        assert 'alpine' in out
        assert '--' in out
        assert 'echo' in out

    def test_passthrough_command_with_global_flags(self):
        """build_passthrough_command preserves global flag ordering."""
        r = parse_args(['--root', '/x', '--remote', 'exec', 'mycontainer', 'ls', '-la'])
        cmd = build_passthrough_command(r, 'podman')
        exec_idx = cmd.index('exec')
        assert cmd.index('--root') < exec_idx
        assert cmd.index('--remote') < exec_idx
        assert 'mycontainer' in cmd
        assert 'ls' in cmd
        assert '-la' in cmd

    def test_store_with_global_podman_flags(self):
        """Podman global flags are parsed but not used in store command."""
        r = parse_args(['--root', '/x', 'store', 'init', '--store-dir', '/s'])
        pga = r.ns.get('podman_global_args') or []
        assert '--root' in pga
        assert r.ns['subcommand'] == 'store'
        assert r.ns['store.action'] == 'init'

    def test_root_flags_before_passthrough_subcommand(self):
        """Root podrun flags + podman global flags + passthrough."""
        r = parse_args(['--print-cmd', '--store', '/s', '--remote', 'images', '--all'])
        assert r.ns['root.print_cmd'] is True
        assert r.ns['root.store'] == '/s'
        pga = r.ns.get('podman_global_args') or []
        assert '--remote' in pga
        assert r.ns['subcommand'] == 'images'
        assert '--all' in r.subcmd_passthrough_args
