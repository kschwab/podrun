import subprocess

import pytest

import os
import pathlib
import shlex
import shutil

from podrun.podrun2 import (
    _PODRUN_STORES_DIR,
    _apply_store,
    _default_store_dir,
    _resolve_store,
    _runroot_path,
    _scrape_podman_help,
    _store_destroy,
    _store_init,
    _store_print_info,
    build_passthrough_command,
    build_root_parser,
    build_run_command,
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
    """Prevent CLI tests from picking up real devcontainer.json or store dirs.

    Also force _is_nested=False so store tests behave consistently
    regardless of the test environment (this container runs inside podrun).
    """
    monkeypatch.setattr(podrun2_mod, 'find_devcontainer_json', lambda start_dir=None: None)
    monkeypatch.setattr(podrun2_mod, '_default_store_dir', lambda: None)
    monkeypatch.setattr(podrun2_mod, '_is_nested', lambda: False)
    # Clear nested podrun guard env var (we're running inside a podrun container)
    monkeypatch.delenv('PODRUN_CONTAINER', raising=False)


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
        """--local-store /path stores the config value; no podman flag translation at parse time."""
        r = parse_args(['--local-store', '/my/store', 'run', 'alpine'])
        assert r.ns['root.local_store'] == '/my/store'

    def test_store_equals_syntax(self):
        r = parse_args(['--local-store=/my/store', 'run', 'alpine'])
        assert r.ns['root.local_store'] == '/my/store'

    def test_store_before_passthrough(self):
        """--local-store before a passthrough subcommand stores config value."""
        r = parse_args(['--local-store', '/my/store', 'ps', '-a'])
        assert r.ns['root.local_store'] == '/my/store'
        assert r.ns['subcommand'] == 'ps'

    def test_store_with_podman_global(self):
        """--local-store and --log-level are independent; --log-level goes to podman_global_args."""
        r = parse_args(['--local-store', '/s', '--log-level', 'debug', 'run', 'alpine'])
        assert r.ns['root.local_store'] == '/s'
        pga = r.ns.get('podman_global_args') or []
        assert '--log-level' in pga
        assert 'debug' in pga

    def test_store_default_none(self):
        r = parse_args(['run', 'alpine'])
        assert r.ns['root.local_store'] is None


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
        """--local-store is a podrun global value flag — argparse consumes it,
        then correctly routes to the 'run' subparser."""
        r = parse_args(['--local-store', '/my/store', 'run', 'alpine'])
        assert r.ns['subcommand'] == 'run'
        assert r.ns['root.local_store'] == '/my/store'
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
        ns, _ = self._parse(['--local-store', '/my/store'])
        assert ns['root.local_store'] == '/my/store'

    def test_ignore_store_flag(self):
        ns, _ = self._parse(['--local-store-ignore'])
        assert ns['root.local_store_ignore'] is True

    def test_auto_init_store_flag(self):
        ns, _ = self._parse(['--local-store-auto-init'])
        assert ns['root.local_store_auto_init'] is True

    def test_store_info_flag(self):
        ns, _ = self._parse(['--local-store-info'])
        assert ns['root.local_store_info'] is True

    def test_store_destroy_flag(self):
        ns, _ = self._parse(['--local-store-destroy'])
        assert ns['root.local_store_destroy'] is True

    def test_store_destroy_default_false(self):
        ns, _ = self._parse([])
        assert ns['root.local_store_destroy'] is False

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
        assert ns['root.local_store'] is None
        assert ns['root.local_store_ignore'] is False
        assert ns['root.local_store_auto_init'] is False
        assert ns['root.local_store_info'] is False


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
# TestEqualsFormParsing — ensure both --flag=value and --flag value forms work
# ---------------------------------------------------------------------------


class TestEqualsFormRootFlags:
    """Verify equals-form parsing for root-level value flags."""

    def _parse(self, args):
        parser = build_root_parser()
        ns, unknowns = parser.parse_known_args(args)
        return vars(ns), unknowns

    def test_config_equals(self):
        ns, _ = self._parse(['--config=/path/to/config.json'])
        assert ns['root.config'] == '/path/to/config.json'

    def test_config_script_equals(self):
        ns, _ = self._parse(['--config-script=/path/to/script'])
        assert ns['root.config_script'] == ['/path/to/script']

    def test_completion_equals(self):
        ns, _ = self._parse(['--completion=bash'])
        assert ns['root.completion'] == 'bash'

    def test_log_level_equals(self):
        ns, _ = self._parse(['--log-level=debug'])
        args = ns.get('podman_global_args') or []
        assert '--log-level' in args
        assert 'debug' in args

    def test_storage_opt_equals(self):
        ns, _ = self._parse(['--storage-opt=ignore_chown_errors=true'])
        args = ns.get('podman_global_args') or []
        assert '--storage-opt' in args
        assert 'ignore_chown_errors=true' in args


class TestEqualsFormRunFlags:
    """Verify equals-form parsing for run-level podrun value flags."""

    def _parse_run(self, args):
        parser = build_root_parser()
        ns, unknowns = parser.parse_known_args(['run'] + args)
        return vars(ns), unknowns

    def test_name_equals(self):
        ns, _ = self._parse_run(['--name=mycontainer'])
        assert ns['run.name'] == 'mycontainer'

    def test_shell_equals(self):
        ns, _ = self._parse_run(['--shell=/bin/zsh'])
        assert ns['run.shell'] == '/bin/zsh'

    def test_prompt_banner_equals(self):
        ns, _ = self._parse_run(['--prompt-banner=DEV'])
        assert ns['run.prompt_banner'] == 'DEV'

    def test_export_equals(self):
        ns, _ = self._parse_run(['--export=/a:/b'])
        assert ns['run.export'] == ['/a:/b']

    def test_export_equals_multiple(self):
        ns, _ = self._parse_run(['--export=/a:/b', '--export=/c:/d:0'])
        assert ns['run.export'] == ['/a:/b', '/c:/d:0']

    def test_label_equals(self):
        ns, _ = self._parse_run(['--label=app=test'])
        assert ns['run.label'] == ['app=test']

    def test_label_short_equals(self):
        ns, _ = self._parse_run(['-l=app=test'])
        assert ns['run.label'] == ['app=test']


class TestEqualsFormPassthroughFlags:
    """Verify equals-form parsing for podman run passthrough value flags."""

    def test_env_short_equals(self):
        r = parse_args(['run', '-e=FOO=bar', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '-e' in pt
        assert 'FOO=bar' in pt

    def test_env_long_space(self):
        r = parse_args(['run', '--env', 'FOO=bar', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '--env' in pt
        assert 'FOO=bar' in pt

    def test_volume_short_equals(self):
        r = parse_args(['run', '-v=/a:/b', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '-v' in pt
        assert '/a:/b' in pt

    def test_volume_long_equals(self):
        r = parse_args(['run', '--volume=/a:/b', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '--volume' in pt
        assert '/a:/b' in pt

    def test_memory_short_equals(self):
        r = parse_args(['run', '-m=512m', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '-m' in pt
        assert '512m' in pt

    def test_memory_long_space(self):
        r = parse_args(['run', '--memory', '512m', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '--memory' in pt
        assert '512m' in pt

    def test_memory_long_equals(self):
        r = parse_args(['run', '--memory=512m', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '--memory' in pt
        assert '512m' in pt

    def test_user_short_equals(self):
        r = parse_args(['run', '-u=1000:1000', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '-u' in pt
        assert '1000:1000' in pt

    def test_user_long_space(self):
        r = parse_args(['run', '--user', '1000:1000', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '--user' in pt
        assert '1000:1000' in pt

    def test_user_long_equals(self):
        r = parse_args(['run', '--user=1000:1000', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '--user' in pt
        assert '1000:1000' in pt

    def test_workdir_short_space(self):
        r = parse_args(['run', '-w', '/app', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '-w' in pt
        assert '/app' in pt

    def test_workdir_short_equals(self):
        r = parse_args(['run', '-w=/app', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '-w' in pt
        assert '/app' in pt

    def test_workdir_long_space(self):
        r = parse_args(['run', '--workdir', '/app', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '--workdir' in pt
        assert '/app' in pt

    def test_workdir_long_equals(self):
        r = parse_args(['run', '--workdir=/app', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '--workdir' in pt
        assert '/app' in pt

    def test_publish_short_equals(self):
        r = parse_args(['run', '-p=8080:80', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '-p' in pt
        assert '8080:80' in pt

    def test_publish_long_space(self):
        r = parse_args(['run', '--publish', '8080:80', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '--publish' in pt
        assert '8080:80' in pt

    def test_publish_long_equals(self):
        r = parse_args(['run', '--publish=8080:80', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '--publish' in pt
        assert '8080:80' in pt

    def test_hostname_short_equals(self):
        r = parse_args(['run', '-h=devbox', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '-h' in pt
        assert 'devbox' in pt

    def test_hostname_long_space(self):
        r = parse_args(['run', '--hostname', 'devbox', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '--hostname' in pt
        assert 'devbox' in pt

    def test_hostname_long_equals(self):
        r = parse_args(['run', '--hostname=devbox', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '--hostname' in pt
        assert 'devbox' in pt

    def test_network_equals(self):
        r = parse_args(['run', '--network=host', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '--network' in pt
        assert 'host' in pt

    def test_mount_equals(self):
        r = parse_args(['run', '--mount=type=bind,src=/a,dst=/b', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '--mount' in pt
        assert 'type=bind,src=/a,dst=/b' in pt

    def test_cpus_equals(self):
        r = parse_args(['run', '--cpus=2', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '--cpus' in pt
        assert '2' in pt

    def test_cap_add_space(self):
        r = parse_args(['run', '--cap-add', 'CAP_CHOWN', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '--cap-add' in pt
        assert 'CAP_CHOWN' in pt

    def test_cap_add_equals(self):
        r = parse_args(['run', '--cap-add=CAP_CHOWN', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '--cap-add' in pt
        assert 'CAP_CHOWN' in pt

    def test_entrypoint_space(self):
        r = parse_args(['run', '--entrypoint', '/bin/sh', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '--entrypoint' in pt
        assert '/bin/sh' in pt

    def test_entrypoint_equals(self):
        r = parse_args(['run', '--entrypoint=/bin/sh', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '--entrypoint' in pt
        assert '/bin/sh' in pt

    def test_userns_space(self):
        r = parse_args(['run', '--userns', 'keep-id', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '--userns' in pt
        assert 'keep-id' in pt

    def test_userns_equals(self):
        r = parse_args(['run', '--userns=keep-id', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '--userns' in pt
        assert 'keep-id' in pt

    def test_annotation_equals(self):
        r = parse_args(['run', '--annotation=note=hello', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '--annotation' in pt
        assert 'note=hello' in pt

    def test_security_opt_space(self):
        r = parse_args(['run', '--security-opt', 'seccomp=unconfined', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '--security-opt' in pt
        assert 'seccomp=unconfined' in pt

    def test_security_opt_equals(self):
        r = parse_args(['run', '--security-opt=seccomp=unconfined', 'alpine'])
        pt = r.ns.get('run.passthrough_args') or []
        assert '--security-opt' in pt
        assert 'seccomp=unconfined' in pt


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
        """--local-store VALUE before 'run' — root parser consumes --store, routes to run."""
        r = parse_args(['--local-store', '/my/store', 'run', 'alpine'])
        assert r.ns['root.local_store'] == '/my/store'
        assert r.ns['subcommand'] == 'run'
        assert 'alpine' in r.trailing_args
        # No translation at parse time — resolution happens in Phase 2
        pga = r.ns.get('podman_global_args') or []
        assert '--root' not in pga

    def test_store_flag_equals_before_subcommand(self):
        r = parse_args(['--local-store=/my/store', 'run', 'alpine'])
        assert r.ns['root.local_store'] == '/my/store'
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
        assert '_podrun' in out

    def test_completion_zsh_exits(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            print_completion('zsh')
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert '#compdef podrun' in out

    def test_completion_fish_exits(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            print_completion('fish')
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert '__podrun_complete' in out


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
        assert '_podrun' in out

    def test_print_cmd_run(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(['--print-cmd', 'run', 'alpine'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'run' in out
        assert 'alpine' in out

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
        """--local-store is config only at parse time; no --root in printed command."""
        with pytest.raises(SystemExit) as exc_info:
            main(['--print-cmd', '--local-store', '/my/store', 'run', 'alpine'])
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
        assert cmd[0] == 'podman'
        assert 'run' in cmd
        assert cmd[-1] == 'alpine'

    def test_run_with_image_and_command(self, capsys):
        cmd = self._cmd(['run', 'alpine', 'bash', '-c', 'echo hi'], capsys)
        assert cmd[0] == 'podman'
        assert 'run' in cmd
        img_idx = cmd.index('alpine')
        assert cmd[img_idx + 1 :] == ['bash', '-c', 'echo hi']

    def test_run_with_separator(self, capsys):
        cmd = self._cmd(['run', 'alpine', '--', 'bash', '-c', 'echo hi'], capsys)
        assert cmd[0] == 'podman'
        assert 'run' in cmd
        sep_idx = cmd.index('--')
        assert cmd[sep_idx + 1 :] == ['bash', '-c', 'echo hi']

    # -- multiple -e flags ---------------------------------------------------

    def test_multiple_env(self, capsys):
        cmd = self._cmd(['run', '-e', 'A=1', '-e', 'B=2', '-e', 'C=3', 'alpine'], capsys)
        assert cmd[0] == 'podman'
        assert 'run' in cmd
        assert 'alpine' in cmd
        # User-provided env vars are present between run and image
        run_idx = cmd.index('run')
        img_idx = cmd.index('alpine')
        mid = cmd[run_idx + 1 : img_idx]
        assert 'A=1' in mid
        assert 'B=2' in mid
        assert 'C=3' in mid

    def test_env_equals_syntax(self, capsys):
        cmd = self._cmd(['run', '--env=FOO=bar', 'alpine'], capsys)
        assert '--env' in cmd
        assert 'FOO=bar' in cmd
        assert 'alpine' in cmd

    # -- multiple -v flags ---------------------------------------------------

    def test_multiple_volume(self, capsys):
        cmd = self._cmd(
            ['run', '-v', '/a:/b', '-v', '/c:/d', '-v', '/e:/f:ro', 'alpine'],
            capsys,
        )
        assert cmd[0] == 'podman'
        assert 'run' in cmd
        assert 'alpine' in cmd
        run_idx = cmd.index('run')
        img_idx = cmd.index('alpine')
        mid = cmd[run_idx + 1 : img_idx]
        assert '/a:/b' in mid
        assert '/c:/d' in mid
        assert '/e:/f:ro' in mid

    def test_volume_long_form(self, capsys):
        cmd = self._cmd(
            ['run', '--volume', '/a:/b', '--volume', '/c:/d', 'alpine'],
            capsys,
        )
        assert cmd[0] == 'podman'
        assert 'run' in cmd
        assert 'alpine' in cmd
        run_idx = cmd.index('run')
        img_idx = cmd.index('alpine')
        mid = cmd[run_idx + 1 : img_idx]
        assert '/a:/b' in mid
        assert '/c:/d' in mid

    # -- mixed env + volume + boolean flags ----------------------------------

    def test_env_volume_rm_privileged(self, capsys):
        cmd = self._cmd(
            [
                'run',
                '--rm',
                '--privileged',
                '-e',
                'A=1',
                '-e',
                'B=2',
                '-v',
                '/host:/ctr',
                '-v',
                '/x:/y:ro',
                'alpine',
            ],
            capsys,
        )
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
        cmd = self._cmd(
            [
                'run',
                '--name',
                'myc',
                '--rm',
                '-e',
                'A=1',
                'alpine',
            ],
            capsys,
        )
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
        cmd = self._cmd(
            [
                '--root',
                '/x',
                '--log-level',
                'debug',
                '--remote',
                'run',
                'alpine',
            ],
            capsys,
        )
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
        assert cmd[0] == 'podman'
        assert 'run' in cmd
        img_idx = cmd.index('alpine')
        assert cmd[img_idx + 1 :] == ['bash', '-c', 'echo hi']
        assert 'A=1' in cmd

    def test_flag_like_command_args(self, capsys):
        """All flag-like tokens after image are passed through literally."""
        cmd = self._cmd(['run', 'alpine', 'ls', '-la', '--color=auto'], capsys)
        assert cmd[0] == 'podman'
        assert 'run' in cmd
        img_idx = cmd.index('alpine')
        assert cmd[img_idx + 1 :] == ['ls', '-la', '--color=auto']

    def test_double_dash_command_with_flags(self, capsys):
        """Explicit '--' separator still works."""
        cmd = self._cmd(['run', '-e', 'A=1', 'alpine', '--', 'bash', '-c', 'echo'], capsys)
        sep_idx = cmd.index('--')
        assert cmd[sep_idx + 1 :] == ['bash', '-c', 'echo']

    # -- rich combinations ---------------------------------------------------

    def test_global_plus_run_flags_plus_env_volume(self, capsys):
        cmd = self._cmd(
            [
                '--root',
                '/x',
                'run',
                '--name',
                'dev',
                '--rm',
                '-e',
                'FOO=bar',
                '-e',
                'BAZ=qux',
                '-v',
                '/a:/b',
                '-v',
                '/c:/d',
                'ubuntu:22.04',
            ],
            capsys,
        )
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
        cmd = self._cmd(
            [
                '--root',
                '/x',
                '--log-level',
                'debug',
                'run',
                '--name',
                'workspace',
                '--rm',
                '--privileged',
                '-e',
                'DISPLAY=:0',
                '-e',
                'HOME=/home/user',
                '-v',
                '/home/user:/home/user',
                '-v',
                '/tmp/.X11-unix:/tmp/.X11-unix',
                '--network',
                'host',
                '-w',
                '/home/user/project',
                'dev-image:latest',
                'bash',
            ],
            capsys,
        )
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
        cmd = self._cmd(
            [
                '--root',
                '/x',
                'run',
                '--name',
                'myc',
                '--rm',
                '-e',
                'A=1',
                '-v',
                '/a:/b',
                'alpine',
                '--',
                'sh',
                '-c',
                'echo done',
            ],
            capsys,
        )
        run_idx = cmd.index('run')
        sep_idx = cmd.index('--')
        img_idx = cmd.index('alpine')
        assert cmd.index('--root') < run_idx
        assert '--name=myc' in cmd
        assert run_idx < img_idx < sep_idx
        assert cmd[sep_idx + 1 :] == ['sh', '-c', 'echo done']

    def test_mount_flags(self, capsys):
        cmd = self._cmd(
            [
                'run',
                '--mount',
                'type=bind,src=/a,dst=/b',
                '--mount',
                'type=tmpfs,dst=/tmp',
                'alpine',
            ],
            capsys,
        )
        assert cmd.count('--mount') == 2
        assert 'type=bind,src=/a,dst=/b' in cmd
        assert 'type=tmpfs,dst=/tmp' in cmd

    def test_label_and_annotation(self, capsys):
        cmd = self._cmd(
            [
                'run',
                '-l',
                'app=test',
                '-l',
                'env=dev',
                '--annotation',
                'note=hello',
                'alpine',
            ],
            capsys,
        )
        assert '--label=app=test' in cmd
        assert '--label=env=dev' in cmd
        assert '--annotation' in cmd
        assert 'note=hello' in cmd

    def test_memory_cpus_user(self, capsys):
        cmd = self._cmd(
            [
                'run',
                '-m',
                '512m',
                '--cpus',
                '2',
                '-u',
                '1000:1000',
                'alpine',
            ],
            capsys,
        )
        assert '-m' in cmd
        assert '512m' in cmd
        assert '--cpus' in cmd
        assert '2' in cmd
        assert '-u' in cmd
        assert '1000:1000' in cmd

    def test_publish_and_network(self, capsys):
        cmd = self._cmd(
            [
                'run',
                '-p',
                '8080:80',
                '-p',
                '443:443',
                '--network',
                'bridge',
                'nginx',
            ],
            capsys,
        )
        assert cmd.count('-p') == 2
        assert '8080:80' in cmd
        assert '443:443' in cmd
        assert '--network' in cmd
        assert 'bridge' in cmd
        assert 'nginx' in cmd


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
        """--local-store is config only; no --root injected at parse time."""
        r = parse_args(['--local-store', '/s', 'run', 'alpine'])
        cmd = build_run_command(r, 'podman')
        assert '--root' not in cmd
        assert cmd == ['podman', 'run', 'alpine']


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

    def test_store_and_auto_init_and_store_info(self):
        r = parse_args(
            [
                '--local-store',
                '/s',
                '--local-store-auto-init',
                '--local-store-info',
                'run',
                'alpine',
            ]
        )
        assert r.ns['root.local_store'] == '/s'
        assert r.ns['root.local_store_auto_init'] is True
        assert r.ns['root.local_store_info'] is True

    def test_store_and_ignore_store(self):
        """Both flags parse — resolution conflict is handled in Phase 2."""
        r = parse_args(['--local-store', '/s', '--local-store-ignore', 'run', 'alpine'])
        assert r.ns['root.local_store'] == '/s'
        assert r.ns['root.local_store_ignore'] is True

    def test_all_root_flags_together(self):
        r = parse_args(
            [
                '--print-cmd',
                '--config',
                '/c.json',
                '--config-script',
                '/s.sh',
                '--no-devconfig',
                '--local-store',
                '/s',
                '--local-store-ignore',
                '--local-store-auto-init',
                '--local-store-info',
                'run',
                'alpine',
            ]
        )
        assert r.ns['root.print_cmd'] is True
        assert r.ns['root.config'] == '/c.json'
        assert r.ns['root.config_script'] == ['/s.sh']
        assert r.ns['root.no_devconfig'] is True
        assert r.ns['root.local_store'] == '/s'
        assert r.ns['root.local_store_ignore'] is True
        assert r.ns['root.local_store_auto_init'] is True
        assert r.ns['root.local_store_info'] is True
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
        r = parse_args(
            [
                '--local-store',
                '/s',
                'run',
                '--name',
                'myc',
                '--adhoc',
                'alpine',
            ]
        )
        assert r.ns['root.local_store'] == '/s'
        assert r.ns['run.name'] == 'myc'
        assert r.ns['run.adhoc'] is True

    def test_ignore_store_before_run_with_shell(self):
        r = parse_args(['--local-store-ignore', 'run', '--shell', '/bin/zsh', 'alpine'])
        assert r.ns['root.local_store_ignore'] is True
        assert r.ns['run.shell'] == '/bin/zsh'

    def test_config_script_before_run_with_export(self):
        r = parse_args(
            [
                '--config-script',
                '/s.sh',
                'run',
                '--export',
                '/a:/b',
                'alpine',
            ]
        )
        assert r.ns['root.config_script'] == ['/s.sh']
        assert r.ns['run.export'] == ['/a:/b']

    def test_print_cmd_with_multiple_run_flags(self):
        r = parse_args(
            [
                '--print-cmd',
                'run',
                '--workspace',
                '--name',
                'myc',
                '--shell',
                '/bin/zsh',
                '--login',
                'alpine',
            ]
        )
        assert r.ns['root.print_cmd'] is True
        assert r.ns['run.workspace'] is True
        assert r.ns['run.name'] == 'myc'
        assert r.ns['run.shell'] == '/bin/zsh'
        assert r.ns['run.login'] is True

    def test_multiple_root_flags_with_multiple_run_flags(self):
        r = parse_args(
            [
                '--print-cmd',
                '--config',
                '/c.json',
                '--no-devconfig',
                '--local-store',
                '/s',
                '--local-store-auto-init',
                'run',
                '--host-overlay',
                '--name',
                'myc',
                '--x11',
                '--export',
                '/a:/b',
                '--export',
                '/c:/d',
                'alpine',
            ]
        )
        assert r.ns['root.print_cmd'] is True
        assert r.ns['root.config'] == '/c.json'
        assert r.ns['root.no_devconfig'] is True
        assert r.ns['root.local_store'] == '/s'
        assert r.ns['root.local_store_auto_init'] is True
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
        r = parse_args(
            [
                'run',
                '--user-overlay',
                '--host-overlay',
                '--interactive-overlay',
                '--workspace',
                '--adhoc',
                'alpine',
            ]
        )
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
        r = parse_args(
            [
                'run',
                '--fuse-overlayfs',
                '--host-overlay',
                '--interactive-overlay',
                'alpine',
            ]
        )
        assert r.ns['run.fuse_overlayfs'] is True
        assert r.ns['run.host_overlay'] is True
        assert r.ns['run.interactive_overlay'] is True

    def test_export_with_workspace_and_name(self):
        r = parse_args(
            [
                'run',
                '--workspace',
                '--name',
                'myc',
                '--export',
                '/src:/dst',
                '--export',
                '/a:/b:0',
                'alpine',
            ]
        )
        assert r.ns['run.workspace'] is True
        assert r.ns['run.name'] == 'myc'
        assert r.ns['run.export'] == ['/src:/dst', '/a:/b:0']

    def test_podman_remote_with_workspace(self):
        r = parse_args(['run', '--podman-remote', '--workspace', 'alpine'])
        assert r.ns['run.podman_remote'] is True
        assert r.ns['run.workspace'] is True

    def test_prompt_banner_with_shell_and_login(self):
        r = parse_args(
            [
                'run',
                '--prompt-banner',
                'DEV',
                '--shell',
                '/bin/zsh',
                '--login',
                'alpine',
            ]
        )
        assert r.ns['run.prompt_banner'] == 'DEV'
        assert r.ns['run.shell'] == '/bin/zsh'
        assert r.ns['run.login'] is True

    def test_all_run_flags_together(self):
        r = parse_args(
            [
                'run',
                '--name',
                'full',
                '--user-overlay',
                '--host-overlay',
                '--interactive-overlay',
                '--workspace',
                '--adhoc',
                '--x11',
                '--podman-remote',
                '--shell',
                '/bin/zsh',
                '--login',
                '--prompt-banner',
                'ALL',
                '--auto-attach',
                '--auto-replace',
                '--export',
                '/a:/b',
                '--fuse-overlayfs',
                '--print-overlays',
                'alpine',
            ]
        )
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
        r = parse_args(
            [
                'run',
                '--name',
                'myc',
                '--host-overlay',
                '-e',
                'A=1',
                '-e',
                'B=2',
                '-e',
                'C=3',
                'alpine',
            ]
        )
        assert r.ns['run.name'] == 'myc'
        assert r.ns['run.host_overlay'] is True
        pt = r.ns.get('run.passthrough_args') or []
        assert pt == ['-e', 'A=1', '-e', 'B=2', '-e', 'C=3']

    def test_mount_with_user_overlay(self):
        r = parse_args(
            [
                'run',
                '--user-overlay',
                '--mount',
                'type=bind,src=/a,dst=/b',
                'alpine',
            ]
        )
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
        r = parse_args(
            [
                'run',
                '--workspace',
                '--fuse-overlayfs',
                '--rm',
                '--privileged',
                '-e',
                'A=1',
                '-v',
                '/x:/y',
                '--network',
                'host',
                '--hostname',
                'dev',
                'alpine',
                'bash',
            ]
        )
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
        r = parse_args(
            [
                'run',
                '--name',
                'limited',
                '-m',
                '512m',
                '--cpus',
                '2',
                'alpine',
            ]
        )
        assert r.ns['run.name'] == 'limited'
        pt = r.ns.get('run.passthrough_args') or []
        assert '-m' in pt
        assert '512m' in pt
        assert '--cpus' in pt
        assert '2' in pt

    def test_label_and_annotation_with_export(self):
        r = parse_args(
            [
                'run',
                '--export',
                '/a:/b',
                '-l',
                'app=test',
                '--annotation',
                'key=val',
                'alpine',
            ]
        )
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
        r = parse_args(
            [
                '--root',
                '/x',
                '--log-level',
                'debug',
                'run',
                '--host-overlay',
                '--name',
                'myc',
                'alpine',
            ]
        )
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
        r = parse_args(
            [
                '--storage-opt',
                'ignore_chown_errors=true',
                'run',
                '--name',
                'myc',
                'alpine',
            ]
        )
        pga = r.ns.get('podman_global_args') or []
        assert '--storage-opt' in pga
        assert 'ignore_chown_errors=true' in pga
        assert r.ns['run.name'] == 'myc'

    def test_multiple_global_flags_with_run_podman_and_podrun_flags(self):
        r = parse_args(
            [
                '--root',
                '/x',
                '--log-level',
                'debug',
                '--remote',
                'run',
                '--workspace',
                '--name',
                'dev',
                '-e',
                'A=1',
                '-v',
                '/a:/b',
                '--rm',
                'alpine',
                'bash',
            ]
        )
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
        r = parse_args(
            [
                '--print-cmd',
                '--config',
                '/c.json',
                '--no-devconfig',
                '--local-store',
                '/s',
                '--local-store-auto-init',
                '--root',
                '/x',
                '--log-level',
                'debug',
                'run',
                '--workspace',
                '--name',
                'full',
                '--shell',
                '/bin/zsh',
                '--login',
                '--x11',
                '--export',
                '/a:/b',
                '--fuse-overlayfs',
                '-e',
                'FOO=bar',
                '-v',
                '/host:/ctr',
                '--rm',
                '--privileged',
                '--network',
                'host',
                'myimage:latest',
                'bash',
                '-c',
                'echo hi',
            ]
        )
        # Root flags
        assert r.ns['root.print_cmd'] is True
        assert r.ns['root.config'] == '/c.json'
        assert r.ns['root.no_devconfig'] is True
        assert r.ns['root.local_store'] == '/s'
        assert r.ns['root.local_store_auto_init'] is True
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
        r = parse_args(
            [
                '--print-cmd',
                '--local-store',
                '/s',
                '--root',
                '/x',
                'run',
                '--adhoc',
                '--name',
                'myc',
                '-e',
                'A=1',
                '--rm',
                'alpine',
                '--',
                'sh',
                '-c',
                'echo done',
            ]
        )
        assert r.ns['root.print_cmd'] is True
        assert r.ns['root.local_store'] == '/s'
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
        r = parse_args(
            [
                '--root',
                '/x',
                '--log-level',
                'debug',
                'run',
                '--name',
                'myc',
                '-e',
                'A=1',
                '-v',
                '/a:/b',
                '--rm',
                'alpine',
                '--',
                'echo',
                'hi',
            ]
        )
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
        assert cmd[sep_idx + 1 :] == ['echo', 'hi']

    def test_build_run_command_boolean_and_value_flags(self):
        r = parse_args(
            [
                'run',
                '--name',
                'myc',
                '--rm',
                '--privileged',
                '-e',
                'A=1',
                '-v',
                '/a:/b',
                '-m',
                '512m',
                'alpine',
            ]
        )
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
            main(
                [
                    '--print-cmd',
                    '--root',
                    '/x',
                    'run',
                    '--name',
                    'myc',
                    '-e',
                    'A=1',
                    '--rm',
                    'alpine',
                    '--',
                    'echo',
                    'hi',
                ]
            )
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

    def test_root_flags_before_passthrough_subcommand(self):
        """Root podrun flags + podman global flags + passthrough."""
        r = parse_args(['--print-cmd', '--local-store', '/s', '--remote', 'images', '--all'])
        assert r.ns['root.print_cmd'] is True
        assert r.ns['root.local_store'] == '/s'
        pga = r.ns.get('podman_global_args') or []
        assert '--remote' in pga
        assert r.ns['subcommand'] == 'images'
        assert '--all' in r.subcmd_passthrough_args


# ---------------------------------------------------------------------------
# Store management — fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def init_store(tmp_path):
    """Create a store directory with graphroot/ and return its path."""
    store_dir = tmp_path / 'store'
    graphroot = store_dir / 'graphroot'
    graphroot.mkdir(parents=True)
    return str(store_dir)


@pytest.fixture
def uninit_store(tmp_path):
    """Create a store directory without graphroot/ and return its path."""
    store_dir = tmp_path / 'store'
    store_dir.mkdir(parents=True)
    return str(store_dir)


# ---------------------------------------------------------------------------
# TestRunrootPath
# ---------------------------------------------------------------------------


class TestRunrootPath:
    def test_deterministic(self):
        """Same input always produces the same output."""
        assert _runroot_path('/a/b/c') == _runroot_path('/a/b/c')

    def test_different_inputs(self):
        """Different graphroots produce different paths."""
        assert _runroot_path('/a') != _runroot_path('/b')

    def test_format(self):
        """Path is _PODRUN_STORES_DIR/<12-char hex hash>."""
        result = _runroot_path('/some/graphroot')
        assert result.startswith(_PODRUN_STORES_DIR + '/')
        suffix = result[len(_PODRUN_STORES_DIR) + 1 :]
        assert len(suffix) == 12
        assert all(c in '0123456789abcdef' for c in suffix)


# ---------------------------------------------------------------------------
# TestDefaultStoreDir — calls the real function (bypasses autouse patch)
# ---------------------------------------------------------------------------


class TestDefaultStoreDir:
    def test_devcontainer_dir(self, tmp_path, monkeypatch):
        """`.devcontainer/` dir → `<root>/.devcontainer/.podrun/store`."""
        (tmp_path / '.devcontainer').mkdir()
        monkeypatch.chdir(tmp_path)
        assert _default_store_dir() == str(tmp_path / '.devcontainer' / '.podrun' / 'store')

    def test_devcontainer_json_file(self, tmp_path, monkeypatch):
        """`.devcontainer.json` file → `<root>/.podrun/store`."""
        (tmp_path / '.devcontainer.json').write_text('{}')
        monkeypatch.chdir(tmp_path)
        assert _default_store_dir() == str(tmp_path / '.podrun' / 'store')

    def test_devcontainer_dir_preferred_over_json(self, tmp_path, monkeypatch):
        """When both markers exist at same level, dir wins."""
        (tmp_path / '.devcontainer').mkdir()
        (tmp_path / '.devcontainer.json').write_text('{}')
        monkeypatch.chdir(tmp_path)
        assert _default_store_dir() == str(tmp_path / '.devcontainer' / '.podrun' / 'store')

    def test_no_project_root(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert _default_store_dir() is None

    def test_parent_walk(self, tmp_path, monkeypatch):
        """Project root in parent directory is found."""
        (tmp_path / '.devcontainer').mkdir()
        child = tmp_path / 'sub' / 'deep'
        child.mkdir(parents=True)
        monkeypatch.chdir(child)
        assert _default_store_dir() == str(tmp_path / '.devcontainer' / '.podrun' / 'store')

    def test_parent_walk_json(self, tmp_path, monkeypatch):
        """Parent with .devcontainer.json found from child."""
        (tmp_path / '.devcontainer.json').write_text('{}')
        child = tmp_path / 'sub'
        child.mkdir()
        monkeypatch.chdir(child)
        assert _default_store_dir() == str(tmp_path / '.podrun' / 'store')


# ---------------------------------------------------------------------------
# TestStoreInit
# ---------------------------------------------------------------------------


class TestStoreInit:
    def test_creates_graphroot(self, tmp_path):
        store_dir = str(tmp_path / 'store')
        _store_init(store_dir)
        assert (tmp_path / 'store' / 'graphroot').is_dir()

    def test_creates_runroot_symlink(self, tmp_path):
        store_dir = str(tmp_path / 'store')
        _store_init(store_dir)
        link = tmp_path / 'store' / 'runroot'
        assert link.is_symlink()
        target = os.readlink(str(link))
        assert target.startswith(_PODRUN_STORES_DIR)

    def test_runroot_target_dir_exists(self, tmp_path):
        store_dir = str(tmp_path / 'store')
        _store_init(store_dir)
        link = tmp_path / 'store' / 'runroot'
        target = os.readlink(str(link))
        assert os.path.isdir(target)

    def test_idempotent(self, tmp_path):
        """Running _store_init twice does not fail."""
        store_dir = str(tmp_path / 'store')
        _store_init(store_dir)
        _store_init(store_dir)
        assert (tmp_path / 'store' / 'graphroot').is_dir()
        assert (tmp_path / 'store' / 'runroot').is_symlink()


# ---------------------------------------------------------------------------
# TestStorePrintInfo
# ---------------------------------------------------------------------------


class TestStorePrintInfo:
    def test_initialized_store(self, tmp_path, capsys):
        store_dir = str(tmp_path / 'store')
        _store_init(store_dir)
        _store_print_info(store_dir)
        out = capsys.readouterr().out
        assert 'Local store:' in out
        assert 'graphroot:' in out
        assert 'runroot:' in out

    def test_uninitialized_store(self, tmp_path, capsys):
        store_dir = tmp_path / 'store'
        store_dir.mkdir()
        _store_print_info(str(store_dir))
        out = capsys.readouterr().out
        assert 'not initialized' in out


# ---------------------------------------------------------------------------
# TestResolveStore — unit tests for _resolve_store code paths
# ---------------------------------------------------------------------------


class TestResolveStore:
    def test_ignore_returns_empty(self):
        """--local-store-ignore → empty flags regardless of store."""
        ns = {'root.local_store_ignore': True, 'root.local_store': '/some/path'}
        flags, env = _resolve_store(ns)
        assert flags == []
        assert env == {}

    def test_ignore_preserves_local_store_value(self):
        """--local-store-ignore does not clear root.local_store."""
        ns = {'root.local_store_ignore': True, 'root.local_store': '/some/path'}
        _resolve_store(ns)
        assert ns['root.local_store'] == '/some/path'

    def test_no_store_no_project_root(self, monkeypatch):
        """No explicit store and _default_store_dir returns None → empty."""
        monkeypatch.setattr(podrun2_mod, '_default_store_dir', lambda: None)
        ns = {}
        flags, env = _resolve_store(ns)
        assert flags == []
        assert ns.get('root.local_store') is None

    def test_explicit_initialized_store(self, init_store):
        """Initialized store → 6-element flags list."""
        ns = {'root.local_store': init_store}
        flags, env = _resolve_store(ns)
        assert len(flags) == 6
        assert flags[0] == '--root'
        assert flags[2] == '--runroot'
        assert flags[4] == '--storage-driver'
        assert flags[5] == 'overlay'

    def test_graphroot_path_in_flags(self, init_store):
        """--root value is the resolved graphroot subdirectory."""
        ns = {'root.local_store': init_store}
        flags, _ = _resolve_store(ns)
        assert flags[1] == str(pathlib.Path(init_store).resolve() / 'graphroot')

    def test_runroot_path_in_flags(self, init_store):
        """--runroot value is under _PODRUN_STORES_DIR."""
        ns = {'root.local_store': init_store}
        flags, _ = _resolve_store(ns)
        assert flags[3].startswith(_PODRUN_STORES_DIR + '/')

    def test_uninitialized_no_auto_init_returns_empty(self, uninit_store):
        """Uninitialized store without auto-init → empty, clears local_store."""
        ns = {'root.local_store': uninit_store}
        flags, env = _resolve_store(ns)
        assert flags == []
        assert ns['root.local_store'] is None

    def test_uninitialized_with_auto_init(self, tmp_path):
        """Uninitialized store with auto-init → creates store, returns flags."""
        store_dir = str(tmp_path / 'new-store')
        ns = {'root.local_store': store_dir, 'root.local_store_auto_init': True}
        flags, env = _resolve_store(ns)
        assert len(flags) == 6
        assert (tmp_path / 'new-store' / 'graphroot').is_dir()

    def test_storage_driver_from_config(self, init_store):
        """root.storage_driver from devcontainer flows to --storage-driver."""
        ns = {'root.local_store': init_store, 'root.storage_driver': 'vfs'}
        flags, _ = _resolve_store(ns)
        idx = flags.index('--storage-driver')
        assert flags[idx + 1] == 'vfs'

    def test_default_driver_is_overlay(self, init_store):
        """No explicit driver → overlay."""
        ns = {'root.local_store': init_store}
        flags, _ = _resolve_store(ns)
        idx = flags.index('--storage-driver')
        assert flags[idx + 1] == 'overlay'

    def test_explicit_storage_driver_respected(self, init_store):
        """--storage-driver in podman_global_args → no duplicate injected."""
        ns = {
            'root.local_store': init_store,
            'podman_global_args': ['--storage-driver', 'vfs'],
        }
        flags, _ = _resolve_store(ns)
        assert '--storage-driver' not in flags
        assert '--root' in flags
        assert '--runroot' in flags

    def test_explicit_storage_driver_over_config(self, init_store):
        """--storage-driver in podman_global_args wins over root.storage_driver."""
        ns = {
            'root.local_store': init_store,
            'root.storage_driver': 'btrfs',
            'podman_global_args': ['--storage-driver', 'vfs'],
        }
        flags, _ = _resolve_store(ns)
        assert '--storage-driver' not in flags

    def test_conflict_root(self, init_store):
        ns = {'root.local_store': init_store, 'podman_global_args': ['--root', '/x']}
        with pytest.raises(SystemExit) as exc_info:
            _resolve_store(ns)
        assert exc_info.value.code == 1

    def test_conflict_runroot(self, init_store):
        ns = {'root.local_store': init_store, 'podman_global_args': ['--runroot', '/x']}
        with pytest.raises(SystemExit) as exc_info:
            _resolve_store(ns)
        assert exc_info.value.code == 1

    def test_auto_discovery_with_project_root(self, tmp_path, monkeypatch):
        """_default_store_dir returns a path with graphroot → flags produced."""
        store_dir = str(tmp_path / '.devcontainer' / '.podrun' / 'store')
        pathlib.Path(store_dir, 'graphroot').mkdir(parents=True)
        monkeypatch.setattr(podrun2_mod, '_default_store_dir', lambda: store_dir)
        ns = {}
        flags, _ = _resolve_store(ns)
        assert len(flags) == 6
        assert ns['root.local_store'] == store_dir

    def test_auto_discovery_uninitialized(self, tmp_path, monkeypatch):
        """_default_store_dir returns a path without graphroot → empty."""
        store_dir = str(tmp_path / '.devcontainer' / '.podrun' / 'store')
        pathlib.Path(store_dir).mkdir(parents=True)
        monkeypatch.setattr(podrun2_mod, '_default_store_dir', lambda: store_dir)
        ns = {}
        flags, _ = _resolve_store(ns)
        assert flags == []
        assert ns['root.local_store'] is None


# ---------------------------------------------------------------------------
# TestApplyStore — integration of _resolve_store into podman_global_args
# ---------------------------------------------------------------------------


class TestApplyStore:
    def test_prepends_flags(self, init_store):
        """Store flags are prepended before existing global args."""
        ns = {'root.local_store': init_store, 'podman_global_args': ['--remote']}
        _apply_store(ns)
        pga = ns['podman_global_args']
        assert pga[0] == '--root'
        assert '--remote' in pga
        assert pga.index('--root') < pga.index('--remote')

    def test_no_flags_no_change(self, monkeypatch):
        """No store resolved → podman_global_args untouched."""
        monkeypatch.setattr(podrun2_mod, '_default_store_dir', lambda: None)
        ns = {'podman_global_args': ['--remote']}
        _apply_store(ns)
        assert ns['podman_global_args'] == ['--remote']

    def test_empty_initial_global_args(self, init_store):
        """Store flags set even when podman_global_args starts empty."""
        ns = {'root.local_store': init_store}
        _apply_store(ns)
        pga = ns['podman_global_args']
        assert pga[0] == '--root'
        assert '--storage-driver' in pga

    def test_ignore_no_flags(self, init_store, monkeypatch):
        """--local-store-ignore → no store flags even with valid store."""
        monkeypatch.setattr(podrun2_mod, '_default_store_dir', lambda: None)
        ns = {
            'root.local_store': init_store,
            'root.local_store_ignore': True,
            'podman_global_args': ['--remote'],
        }
        _apply_store(ns)
        assert ns['podman_global_args'] == ['--remote']


# ---------------------------------------------------------------------------
# TestStoreCommandIntegration — full pipeline via main() --print-cmd
# ---------------------------------------------------------------------------


class TestStoreCommandIntegration:
    """Verify store flags in the final podman command through main()."""

    def _cmd(self, argv, capsys):
        """Run main(['--print-cmd'] + argv) and return printed command tokens."""
        with pytest.raises(SystemExit) as exc_info:
            main(['--print-cmd'] + argv)
        assert exc_info.value.code == 0
        tokens = shlex.split(capsys.readouterr().out)
        if tokens and tokens[0].endswith('podman'):
            tokens[0] = 'podman'
        return tokens

    # -- run subcommand with store ------------------------------------------

    def test_run_with_store(self, init_store, capsys):
        """Initialized store → --root, --runroot, --storage-driver before 'run'."""
        cmd = self._cmd(['--local-store', init_store, 'run', 'alpine'], capsys)
        assert '--root' in cmd
        assert '--runroot' in cmd
        assert '--storage-driver' in cmd
        run_idx = cmd.index('run')
        assert cmd.index('--root') < run_idx
        assert cmd.index('--runroot') < run_idx
        assert cmd.index('--storage-driver') < run_idx
        assert cmd[-1] == 'alpine'

    def test_run_default_driver_overlay(self, init_store, capsys):
        """Default --storage-driver is overlay."""
        cmd = self._cmd(['--local-store', init_store, 'run', 'alpine'], capsys)
        idx = cmd.index('--storage-driver')
        assert cmd[idx + 1] == 'overlay'

    def test_run_explicit_storage_driver(self, init_store, capsys):
        """--storage-driver passed as podman global flag is respected."""
        cmd = self._cmd(
            [
                '--local-store',
                init_store,
                '--storage-driver',
                'vfs',
                'run',
                'alpine',
            ],
            capsys,
        )
        # --storage-driver vfs appears once (from passthrough), not duplicated by store
        assert cmd.count('--storage-driver') == 1
        idx = cmd.index('--storage-driver')
        assert cmd[idx + 1] == 'vfs'

    def test_run_store_with_name_and_passthrough(self, init_store, capsys):
        """Store flags + named flags + passthrough all appear correctly."""
        cmd = self._cmd(
            [
                '--local-store',
                init_store,
                'run',
                '--name',
                'myc',
                '-e',
                'A=1',
                'alpine',
            ],
            capsys,
        )
        assert '--root' in cmd
        assert '--name=myc' in cmd
        assert '-e' in cmd
        assert 'A=1' in cmd
        run_idx = cmd.index('run')
        assert cmd.index('--root') < run_idx

    # -- passthrough subcommands with store ---------------------------------

    def test_ps_with_store(self, init_store, capsys):
        """Store flags appear before 'ps' in passthrough command."""
        cmd = self._cmd(['--local-store', init_store, 'ps', '-a'], capsys)
        assert '--root' in cmd
        assert '--runroot' in cmd
        assert '--storage-driver' in cmd
        ps_idx = cmd.index('ps')
        assert cmd.index('--root') < ps_idx
        assert '-a' in cmd

    def test_images_with_store(self, init_store, capsys):
        """Store flags appear before 'images' in passthrough command."""
        cmd = self._cmd(['--local-store', init_store, 'images', '--all'], capsys)
        assert '--root' in cmd
        assert '--storage-driver' in cmd
        images_idx = cmd.index('images')
        assert cmd.index('--root') < images_idx
        assert '--all' in cmd

    def test_build_with_store(self, init_store, capsys):
        """Store flags appear before 'build' in passthrough command."""
        cmd = self._cmd(['--local-store', init_store, 'build', '.'], capsys)
        assert '--root' in cmd
        build_idx = cmd.index('build')
        assert cmd.index('--root') < build_idx

    def test_rmi_with_store(self, init_store, capsys):
        """Store flags appear before 'rmi' in passthrough command."""
        cmd = self._cmd(['--local-store', init_store, 'rmi', 'alpine'], capsys)
        assert '--root' in cmd
        rmi_idx = cmd.index('rmi')
        assert cmd.index('--root') < rmi_idx

    def test_passthrough_explicit_storage_driver(self, init_store, capsys):
        """--storage-driver as podman global flag flows to passthrough subcommands."""
        cmd = self._cmd(
            [
                '--local-store',
                init_store,
                '--storage-driver',
                'vfs',
                'ps',
            ],
            capsys,
        )
        assert cmd.count('--storage-driver') == 1
        idx = cmd.index('--storage-driver')
        assert cmd[idx + 1] == 'vfs'

    # -- --local-store-ignore suppresses flags ------------------------------

    def test_ignore_run(self, init_store, capsys):
        """--local-store-ignore → no store flags in run command."""
        cmd = self._cmd(
            [
                '--local-store',
                init_store,
                '--local-store-ignore',
                'run',
                'alpine',
            ],
            capsys,
        )
        assert '--root' not in cmd
        assert '--runroot' not in cmd
        assert '--storage-driver' not in cmd
        assert cmd[0] == 'podman'
        assert 'run' in cmd
        assert cmd[-1] == 'alpine'

    def test_ignore_passthrough(self, init_store, capsys):
        """--local-store-ignore → no store flags in passthrough command."""
        cmd = self._cmd(
            [
                '--local-store',
                init_store,
                '--local-store-ignore',
                'ps',
                '-a',
            ],
            capsys,
        )
        assert '--root' not in cmd
        assert '--runroot' not in cmd
        assert '--storage-driver' not in cmd

    def test_ignore_with_other_global_flags(self, init_store, capsys):
        """--local-store-ignore doesn't suppress non-store global flags."""
        cmd = self._cmd(
            [
                '--local-store',
                init_store,
                '--local-store-ignore',
                '--remote',
                'run',
                'alpine',
            ],
            capsys,
        )
        assert '--root' not in cmd
        assert '--remote' in cmd

    # -- uninitialized store (no --local-store-auto-init) -------------------

    def test_uninitialized_store_no_flags_run(self, uninit_store, capsys):
        """Uninitialized store without auto-init → no store flags."""
        cmd = self._cmd(['--local-store', uninit_store, 'run', 'alpine'], capsys)
        assert '--root' not in cmd
        assert '--runroot' not in cmd
        assert '--storage-driver' not in cmd

    def test_uninitialized_store_no_flags_passthrough(self, uninit_store, capsys):
        """Uninitialized store without auto-init → no store flags on passthrough."""
        cmd = self._cmd(['--local-store', uninit_store, 'ps'], capsys)
        assert '--root' not in cmd

    def test_nonexistent_store_no_flags(self, tmp_path, capsys):
        """Store path that doesn't exist → no store flags."""
        cmd = self._cmd(
            [
                '--local-store',
                str(tmp_path / 'nonexistent'),
                'run',
                'alpine',
            ],
            capsys,
        )
        assert '--root' not in cmd

    # -- --local-store-auto-init --------------------------------------------

    def test_auto_init_run(self, tmp_path, capsys):
        """Auto-init creates store and injects flags into run command."""
        store_dir = str(tmp_path / 'new-store')
        cmd = self._cmd(
            [
                '--local-store',
                store_dir,
                '--local-store-auto-init',
                'run',
                'alpine',
            ],
            capsys,
        )
        assert '--root' in cmd
        assert '--runroot' in cmd
        assert '--storage-driver' in cmd
        # Verify store created on disk
        assert (tmp_path / 'new-store' / 'graphroot').is_dir()

    def test_auto_init_passthrough(self, tmp_path, capsys):
        """Auto-init creates store and injects flags into passthrough command."""
        store_dir = str(tmp_path / 'new-store')
        cmd = self._cmd(
            [
                '--local-store',
                store_dir,
                '--local-store-auto-init',
                'ps',
            ],
            capsys,
        )
        assert '--root' in cmd
        assert (tmp_path / 'new-store' / 'graphroot').is_dir()

    def test_auto_init_explicit_storage_driver(self, tmp_path, capsys):
        """Auto-init with explicit --storage-driver flows through."""
        store_dir = str(tmp_path / 'new-store')
        cmd = self._cmd(
            [
                '--local-store',
                store_dir,
                '--local-store-auto-init',
                '--storage-driver',
                'vfs',
                'run',
                'alpine',
            ],
            capsys,
        )
        assert cmd.count('--storage-driver') == 1
        idx = cmd.index('--storage-driver')
        assert cmd[idx + 1] == 'vfs'

    # -- no project root, no explicit store ---------------------------------

    def test_no_store_no_project_root_run(self, capsys):
        """No store configured → no store flags in run output."""
        cmd = self._cmd(['run', 'alpine'], capsys)
        assert '--root' not in cmd
        assert '--runroot' not in cmd
        assert '--storage-driver' not in cmd

    def test_no_store_no_project_root_passthrough(self, capsys):
        """No store configured → no store flags in passthrough output."""
        cmd = self._cmd(['ps', '-a'], capsys)
        assert '--root' not in cmd

    # -- store flags with other podman global flags -------------------------

    def test_store_and_remote_run(self, init_store, capsys):
        """Store flags + --remote both appear before 'run'."""
        cmd = self._cmd(
            [
                '--local-store',
                init_store,
                '--remote',
                'run',
                'alpine',
            ],
            capsys,
        )
        run_idx = cmd.index('run')
        assert cmd.index('--root') < run_idx
        assert cmd.index('--remote') < run_idx

    def test_store_and_log_level_passthrough(self, init_store, capsys):
        """Store flags + --log-level both appear before subcommand."""
        cmd = self._cmd(
            [
                '--local-store',
                init_store,
                '--log-level',
                'debug',
                'ps',
            ],
            capsys,
        )
        ps_idx = cmd.index('ps')
        assert cmd.index('--root') < ps_idx
        assert cmd.index('--log-level') < ps_idx

    # -- conflict detection -------------------------------------------------

    def test_conflict_root(self, init_store, capsys):
        """--root + active store → error exit."""
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    '--print-cmd',
                    '--local-store',
                    init_store,
                    '--root',
                    '/other',
                    'run',
                    'alpine',
                ]
            )
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert 'conflicts' in err
        assert '--root' in err

    def test_conflict_runroot(self, init_store, capsys):
        """--runroot + active store → error exit."""
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    '--print-cmd',
                    '--local-store',
                    init_store,
                    '--runroot',
                    '/other',
                    'run',
                    'alpine',
                ]
            )
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert 'conflicts' in err

    def test_storage_driver_respected_with_store(self, init_store, capsys):
        """--storage-driver + active store → driver respected, no conflict."""
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    '--print-cmd',
                    '--local-store',
                    init_store,
                    '--storage-driver',
                    'vfs',
                    'run',
                    'alpine',
                ]
            )
        assert exc_info.value.code == 0
        cmd = capsys.readouterr().out.strip().split()
        assert cmd.count('--storage-driver') == 1
        idx = cmd.index('--storage-driver')
        assert cmd[idx + 1] == 'vfs'

    def test_conflict_root_passthrough(self, init_store, capsys):
        """--root + active store → error exit (passthrough subcommand too)."""
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    '--print-cmd',
                    '--local-store',
                    init_store,
                    '--root',
                    '/other',
                    'ps',
                ]
            )
        assert exc_info.value.code == 1

    def test_no_conflict_when_ignore(self, init_store, capsys):
        """--local-store-ignore prevents conflict even with --root."""
        cmd = self._cmd(
            [
                '--local-store',
                init_store,
                '--local-store-ignore',
                '--root',
                '/other',
                'run',
                'alpine',
            ],
            capsys,
        )
        # --root passes through as normal podman flag
        assert '--root' in cmd
        assert '/other' in cmd
        # No store flags injected
        assert cmd.count('--root') == 1

    def test_no_conflict_when_uninitialized(self, uninit_store, capsys):
        """Uninitialized store + --root → no conflict (store not active)."""
        cmd = self._cmd(
            [
                '--local-store',
                uninit_store,
                '--root',
                '/other',
                'run',
                'alpine',
            ],
            capsys,
        )
        assert '--root' in cmd
        assert '/other' in cmd

    # -- --local-store-info -------------------------------------------------

    def test_store_info_initialized(self, tmp_path, capsys):
        """--local-store-info with initialized store → prints info, exits 0."""
        store_dir = str(tmp_path / 'store')
        _store_init(store_dir)
        with pytest.raises(SystemExit) as exc_info:
            main(['--local-store', store_dir, '--local-store-info'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'Local store:' in out
        assert 'graphroot:' in out

    def test_store_info_uninitialized(self, uninit_store, capsys):
        """--local-store-info with uninitialized store → no store configured."""
        with pytest.raises(SystemExit) as exc_info:
            main(['--local-store', uninit_store, '--local-store-info'])
        assert exc_info.value.code == 0
        err = capsys.readouterr().err
        assert 'No local store configured' in err

    def test_store_info_no_store(self, capsys):
        """--local-store-info with no store at all → no store configured."""
        with pytest.raises(SystemExit) as exc_info:
            main(['--local-store-info'])
        assert exc_info.value.code == 0
        err = capsys.readouterr().err
        assert 'No local store configured' in err

    def test_store_info_exits_before_subcommand(self, init_store, capsys):
        """--local-store-info exits before executing any subcommand."""
        _store_init(init_store)
        with pytest.raises(SystemExit) as exc_info:
            main(['--local-store', init_store, '--local-store-info', 'run', 'alpine'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'Local store:' in out

    # -- podman remote ------------------------------------------------------

    def test_nested_no_store_flags(self, init_store, monkeypatch):
        """Nested (inside podrun container) → _apply_store skips store flags."""
        monkeypatch.setattr(podrun2_mod, '_is_nested', lambda: True)
        ns = {'root.local_store': init_store}
        _apply_store(ns, 'podman')
        assert 'podman_global_args' not in ns or '--root' not in (
            ns.get('podman_global_args') or []
        )

    def test_nested_store_info(self, init_store, capsys, monkeypatch):
        """Nested + --local-store-info → disabled message."""
        monkeypatch.setattr(podrun2_mod, '_is_nested', lambda: True)
        ns = {'root.local_store': init_store, 'root.local_store_info': True}
        with pytest.raises(SystemExit) as exc_info:
            _apply_store(ns, 'podman')
        assert exc_info.value.code == 0
        err = capsys.readouterr().err
        assert 'disabled' in err
        assert 'remote' in err


# ---------------------------------------------------------------------------
# TestStoreDestroy — _store_destroy unit tests
# ---------------------------------------------------------------------------


class TestStoreDestroy:
    def test_destroy_nonexistent_store(self, tmp_path, monkeypatch):
        """Store dir doesn't exist → returns early (no-op)."""
        store_dir = str(tmp_path / 'nonexistent')
        # Should not raise
        _store_destroy(store_dir, 'podman')

    def test_destroy_removes_store_dir(self, tmp_path, monkeypatch, capsys):
        """Store dir is removed after destroy."""
        store_dir = str(tmp_path / 'store')
        _store_init(store_dir)
        # Mock subprocess.run to avoid calling real podman
        monkeypatch.setattr(
            'subprocess.run',
            lambda *a, **kw: subprocess.CompletedProcess(args=a, returncode=0),
        )
        _store_destroy(store_dir, 'podman')
        assert not pathlib.Path(store_dir).exists()

    def test_destroy_removes_runroot_target(self, tmp_path, monkeypatch, capsys):
        """Runroot target directory is removed."""
        store_dir = str(tmp_path / 'store')
        _store_init(store_dir)
        runroot_link = pathlib.Path(store_dir) / 'runroot'
        runroot_target = os.readlink(str(runroot_link))
        assert os.path.isdir(runroot_target)
        monkeypatch.setattr(
            'subprocess.run',
            lambda *a, **kw: subprocess.CompletedProcess(args=a, returncode=0),
        )
        _store_destroy(store_dir, 'podman')
        assert not os.path.exists(runroot_target)

    def test_destroy_cleans_empty_parent(self, tmp_path, monkeypatch, capsys):
        """Parent /tmp/podrun-stores/ is removed if empty after destroy."""
        store_dir = str(tmp_path / 'store')
        _store_init(store_dir)
        monkeypatch.setattr(
            'subprocess.run',
            lambda *a, **kw: subprocess.CompletedProcess(args=a, returncode=0),
        )
        parent = pathlib.Path(_PODRUN_STORES_DIR)
        # Only test parent cleanup if no other stores exist
        if parent.exists() and not any(
            p
            for p in parent.iterdir()
            if str(p) != str(pathlib.Path(store_dir).resolve() / 'runroot')
        ):
            _store_destroy(store_dir, 'podman')
            out = capsys.readouterr().out
            # If parent was empty after cleanup, it should be removed
            if f'Removed {parent}' in out:
                assert not parent.exists()
        else:
            _store_destroy(store_dir, 'podman')

    def test_destroy_shutil_fallback_to_unshare(self, tmp_path, monkeypatch, capsys):
        """PermissionError on shutil.rmtree → falls back to podman unshare rm -rf."""
        store_dir = str(tmp_path / 'store')
        _store_init(store_dir)

        calls = []
        original_rmtree = shutil.rmtree

        def mock_rmtree(path, *args, **kwargs):
            p = str(path)
            if p == str(pathlib.Path(store_dir).resolve()):
                raise PermissionError('mock')
            return original_rmtree(p, *args, **kwargs)

        def mock_subprocess_run(*args, **kwargs):
            cmd = args[0] if args else kwargs.get('args', [])
            calls.append(cmd)
            # For the unshare fallback, actually remove the dir
            if isinstance(cmd, list) and 'unshare' in cmd:
                original_rmtree(str(pathlib.Path(store_dir).resolve()))
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        monkeypatch.setattr('shutil.rmtree', mock_rmtree)
        monkeypatch.setattr('subprocess.run', mock_subprocess_run)
        _store_destroy(store_dir, 'podman')
        # Verify unshare was called
        unshare_calls = [c for c in calls if isinstance(c, list) and 'unshare' in c]
        assert len(unshare_calls) == 1
        assert 'rm' in unshare_calls[0]
        assert '-rf' in unshare_calls[0]

    def test_destroy_podman_reset_called(self, tmp_path, monkeypatch, capsys):
        """podman system reset --force is invoked per graphroot."""
        store_dir = str(tmp_path / 'store')
        _store_init(store_dir)

        calls = []

        def mock_subprocess_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        monkeypatch.setattr('subprocess.run', mock_subprocess_run)
        _store_destroy(store_dir, 'podman')
        reset_calls = [c for c in calls if isinstance(c, list) and 'system' in c and 'reset' in c]
        assert len(reset_calls) >= 1
        for call in reset_calls:
            assert '--force' in call
            assert '--root' in call
            assert '--storage-driver' in call

    def test_destroy_broken_symlink(self, tmp_path, monkeypatch, capsys):
        """Handles missing/broken runroot symlink gracefully."""
        store_dir = str(tmp_path / 'store')
        store_path = pathlib.Path(store_dir)
        graphroot = store_path / 'graphroot'
        graphroot.mkdir(parents=True)
        # Create a broken symlink
        runroot_link = store_path / 'runroot'
        runroot_link.symlink_to('/nonexistent/broken/target')

        monkeypatch.setattr(
            'subprocess.run',
            lambda *a, **kw: subprocess.CompletedProcess(args=a, returncode=0),
        )
        _store_destroy(store_dir, 'podman')
        assert not store_path.exists()

    def test_destroy_skips_non_dir_in_graphroot_glob(self, tmp_path, monkeypatch, capsys):
        """Non-directory graphroot* entries are skipped."""
        store_dir = str(tmp_path / 'store')
        store_path = pathlib.Path(store_dir)
        graphroot = store_path / 'graphroot'
        graphroot.mkdir(parents=True)
        # Create a file that matches graphroot*
        (store_path / 'graphroot-file').write_text('not a dir')

        calls = []

        def mock_subprocess_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        monkeypatch.setattr('subprocess.run', mock_subprocess_run)
        _store_destroy(store_dir, 'podman')
        # Only one reset call for graphroot dir, not for graphroot-file
        reset_calls = [c for c in calls if isinstance(c, list) and 'system' in c and 'reset' in c]
        assert len(reset_calls) == 1


# ---------------------------------------------------------------------------
# TestStoreDestroyIntegration — full pipeline via main()
# ---------------------------------------------------------------------------


class TestStoreDestroyIntegration:
    """Verify --local-store-destroy through main().

    Integration tests mock ``_store_destroy`` at the module level so that
    ``main()`` can still call ``get_podman_version`` / ``subprocess.run``
    normally.  The mock performs a simple ``shutil.rmtree`` to simulate
    store removal without invoking podman.
    """

    @staticmethod
    def _fake_destroy(store_dir, podman_path):
        """Lightweight stand-in that removes the dir without podman."""
        store_path = pathlib.Path(store_dir).resolve()
        if store_path.exists():
            shutil.rmtree(str(store_path))

    def test_store_destroy_initialized_no_subcommand(self, tmp_path, capsys, monkeypatch):
        """Destroy initialized store with no subcommand → exits 0."""
        store_dir = str(tmp_path / 'store')
        _store_init(store_dir)
        monkeypatch.setattr(podrun2_mod, '_store_destroy', self._fake_destroy)
        with pytest.raises(SystemExit) as exc_info:
            main(['--local-store', store_dir, '--local-store-destroy'])
        assert exc_info.value.code == 0

    def test_store_destroy_no_store_configured(self, capsys):
        """No store configured → no error, exits 0 (no subcommand)."""
        with pytest.raises(SystemExit) as exc_info:
            main(['--local-store-destroy'])
        assert exc_info.value.code == 0

    def test_store_destroy_nonexistent_dir(self, tmp_path, capsys, monkeypatch):
        """Store dir doesn't exist on disk → no error, exits 0."""
        store_dir = str(tmp_path / 'nonexistent')
        monkeypatch.setattr(podrun2_mod, '_store_destroy', self._fake_destroy)
        with pytest.raises(SystemExit) as exc_info:
            main(['--local-store', store_dir, '--local-store-destroy'])
        assert exc_info.value.code == 0

    def test_store_destroy_nested_error(self, capsys, monkeypatch):
        """Nested (inside podrun) + --local-store-destroy → exits 1 with error."""
        monkeypatch.setattr(podrun2_mod, '_is_nested', lambda: True)
        ns = {'root.local_store_destroy': True}
        with pytest.raises(SystemExit) as exc_info:
            _apply_store(ns, 'podman')
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert '--local-store-destroy' in err
        assert 'remote' in err

    def test_store_destroy_then_auto_init_run(self, tmp_path, capsys, monkeypatch):
        """Destroy + auto-init + run → store recreated, flags injected in command."""
        store_dir = str(tmp_path / 'store')
        _store_init(store_dir)
        monkeypatch.setattr(podrun2_mod, '_store_destroy', self._fake_destroy)
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    '--print-cmd',
                    '--local-store',
                    store_dir,
                    '--local-store-destroy',
                    '--local-store-auto-init',
                    'run',
                    'alpine',
                ]
            )
        assert exc_info.value.code == 0
        tokens = shlex.split(capsys.readouterr().out)
        assert '--root' in tokens
        assert '--runroot' in tokens
        assert '--storage-driver' in tokens
        # Verify store was recreated on disk
        assert (tmp_path / 'store' / 'graphroot').is_dir()

    def test_store_destroy_then_run_no_auto_init(self, tmp_path, capsys, monkeypatch):
        """Destroy + run without auto-init → no store flags in command."""
        store_dir = str(tmp_path / 'store')
        _store_init(store_dir)
        monkeypatch.setattr(podrun2_mod, '_store_destroy', self._fake_destroy)
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    '--print-cmd',
                    '--local-store',
                    store_dir,
                    '--local-store-destroy',
                    'run',
                    'alpine',
                ]
            )
        assert exc_info.value.code == 0
        tokens = shlex.split(capsys.readouterr().out)
        assert '--root' not in tokens
        assert '--runroot' not in tokens
        assert '--storage-driver' not in tokens

    def test_store_destroy_then_info(self, tmp_path, capsys, monkeypatch):
        """Destroy + info → info shows no store / 'not initialized'."""
        store_dir = str(tmp_path / 'store')
        _store_init(store_dir)
        monkeypatch.setattr(podrun2_mod, '_store_destroy', self._fake_destroy)
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    '--local-store',
                    store_dir,
                    '--local-store-destroy',
                    '--local-store-info',
                ]
            )
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        # After destroy, info should show no store configured or not initialized
        assert 'No local store configured' in captured.err or 'not initialized' in captured.out

    def test_store_destroy_then_auto_init_then_info(self, tmp_path, capsys, monkeypatch):
        """Destroy + auto-init + info → info shows fresh store."""
        store_dir = str(tmp_path / 'store')
        _store_init(store_dir)
        monkeypatch.setattr(podrun2_mod, '_store_destroy', self._fake_destroy)
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    '--local-store',
                    store_dir,
                    '--local-store-destroy',
                    '--local-store-auto-init',
                    '--local-store-info',
                ]
            )
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'Local store:' in out
        assert 'graphroot:' in out
