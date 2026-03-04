import subprocess

import pytest

from podrun.podrun import (
    _expand_config_scripts,
    _scrape_podman_value_flags,
    check_flags,
    parse_cli_args,
    _build_parser,
    _PodrunParser,
    PODMAN_RUN_VALUE_FLAGS,
)


class TestExpandConfigScripts:
    def test_no_scripts_passthrough(self, mock_run_os_cmd):
        result, found = _expand_config_scripts(['--name', 'test', 'image'])
        assert result == ['--name', 'test', 'image']
        assert found is False

    def test_equals_syntax(self, mock_run_os_cmd):
        mock_run_os_cmd.set_return(stdout='--rm --privileged')
        result, found = _expand_config_scripts(['--config-script=/path/to/script'])
        assert '--rm' in result
        assert '--privileged' in result
        assert found is True

    def test_space_syntax(self, mock_run_os_cmd):
        mock_run_os_cmd.set_return(stdout='--rm')
        result, found = _expand_config_scripts(['--config-script', '/path/to/script'])
        assert '--rm' in result
        assert found is True

    def test_preserves_surrounding_args(self, mock_run_os_cmd):
        mock_run_os_cmd.set_return(stdout='--rm')
        result, found = _expand_config_scripts(
            ['--name', 'test', '--config-script=/script', '--init']
        )
        assert result == ['--name', 'test', '--rm', '--init']

    def test_after_separator_not_expanded(self, mock_run_os_cmd):
        result, found = _expand_config_scripts(['--', '--config-script=/script'])
        assert '--config-script=/script' in result
        assert found is False
        assert len(mock_run_os_cmd.calls) == 0

    def test_failure_exits(self, mock_run_os_cmd):
        mock_run_os_cmd.set_return(returncode=1, stderr='fail')
        with pytest.raises(SystemExit):
            _expand_config_scripts(['--config-script=/bad'])


class TestParseCliArgs:
    def test_image_as_trailing(self, mock_run_os_cmd):
        args = parse_cli_args(['alpine'])
        assert 'alpine' in args.trailing_args

    def test_name_flag(self, mock_run_os_cmd):
        args = parse_cli_args(['--name', 'myname', 'alpine'])
        assert args.name == 'myname'

    def test_user_overlay(self, mock_run_os_cmd):
        args = parse_cli_args(['--user-overlay', 'alpine'])
        assert args.user_overlay is True

    def test_passthrough_flags(self, mock_run_os_cmd):
        args = parse_cli_args(['--rm', '--privileged', 'alpine'])
        assert '--rm' in args.passthrough_args
        assert '--privileged' in args.passthrough_args

    def test_explicit_command_after_separator(self, mock_run_os_cmd):
        args = parse_cli_args(['alpine', '--', 'bash', '-c', 'echo hi'])
        assert args.explicit_command == ['bash', '-c', 'echo hi']

    def test_version_exits(self, mock_run_os_cmd):
        with pytest.raises(SystemExit):
            parse_cli_args(['--version'])

    def test_value_flag_consumption(self, mock_run_os_cmd):
        args = parse_cli_args(['-v', '/src:/dest', 'alpine'])
        assert '-v' in args.passthrough_args
        assert '/src:/dest' in args.passthrough_args
        assert 'alpine' in args.trailing_args

    def test_global_value_flag_consumption(self, mock_run_os_cmd):
        """Global podman flags (--root, --runroot, etc.) consume their values."""
        args = parse_cli_args(
            [
                '--root',
                '/store/graphroot',
                '--runroot',
                '/tmp/runroot',
                '--storage-driver',
                'overlay',
                '--storage-opt',
                'overlay.ignore_chown_errors=true',
                'alpine',
            ]
        )
        assert '--root' in args.passthrough_args
        assert '/store/graphroot' in args.passthrough_args
        assert '--runroot' in args.passthrough_args
        assert '/tmp/runroot' in args.passthrough_args
        assert '--storage-driver' in args.passthrough_args
        assert 'overlay' in args.passthrough_args
        assert args.trailing_args == ['alpine']

    def test_help_flag_passes_through(self, mock_run_os_cmd, capsys):
        """--help is not handled by the parser; it passes through as unknown.

        Help is handled by _print_help() in main() before parse_cli_args
        is called, so the parser never sees -h/--help.
        """
        args = parse_cli_args(['--help', 'alpine'])
        assert '--help' in args.passthrough_args

    def test_check_flags_triggers(self, mock_run_os_cmd):
        """Cover the check_flags() call in parse_cli_args (line 512)."""
        # check_flags calls run_os_cmd twice then sys.exit
        mock_run_os_cmd.set_side_effect(
            [
                subprocess.CompletedProcess(
                    args='', returncode=0, stdout='podman version 4.5.0\n', stderr=''
                ),
                subprocess.CompletedProcess(args='', returncode=1, stdout='', stderr=''),
            ]
        )
        with pytest.raises(SystemExit):
            parse_cli_args(['--check-flags'])


class TestFuseOverlayfsFlag:
    def test_fuse_overlayfs_sets_true(self, mock_run_os_cmd):
        args = parse_cli_args(['--fuse-overlayfs', 'alpine'])
        assert args.fuse_overlayfs is True

    def test_fuse_overlayfs_default_none(self, mock_run_os_cmd):
        args = parse_cli_args(['alpine'])
        assert args.fuse_overlayfs is None


class TestLoginFlag:
    def test_login_sets_true(self, mock_run_os_cmd):
        args = parse_cli_args(['--login', 'alpine'])
        assert args.login is True

    def test_no_login_sets_false(self, mock_run_os_cmd):
        args = parse_cli_args(['--no-login', 'alpine'])
        assert args.login is False

    def test_neither_sets_none(self, mock_run_os_cmd):
        args = parse_cli_args(['alpine'])
        assert args.login is None


class TestWorkspaceAdhoc:
    def test_workspace_alone(self, mock_run_os_cmd):
        args = parse_cli_args(['--workspace', 'alpine'])
        assert args.workspace is True
        assert args.adhoc is None

    def test_adhoc_alone(self, mock_run_os_cmd):
        args = parse_cli_args(['--adhoc', 'alpine'])
        assert args.adhoc is True

    def test_workspace_and_adhoc_together(self, mock_run_os_cmd):
        """--workspace --adhoc is allowed (adhoc implies workspace)."""
        args = parse_cli_args(['--workspace', '--adhoc', 'alpine'])
        assert args.workspace is True
        assert args.adhoc is True


class TestCompletion:
    def test_completion_bash_exits(self, mock_run_os_cmd, capsys):
        """--completion bash prints script and exits."""
        with pytest.raises(SystemExit) as exc_info:
            parse_cli_args(['--completion', 'bash'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert '_podrun' in out
        assert 'complete' in out

    def test_completion_zsh_exits(self, mock_run_os_cmd, capsys):
        """--completion zsh prints script and exits."""
        with pytest.raises(SystemExit) as exc_info:
            parse_cli_args(['--completion', 'zsh'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'podrun' in out
        assert 'compdef' in out

    def test_completion_fish_exits(self, mock_run_os_cmd, capsys):
        """--completion fish prints script and exits."""
        with pytest.raises(SystemExit) as exc_info:
            parse_cli_args(['--completion', 'fish'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert 'podrun' in out
        assert 'complete -c podrun' in out

    def test_completion_invalid_shell(self, mock_run_os_cmd):
        """--completion invalid → argparse error."""
        with pytest.raises(SystemExit) as exc_info:
            parse_cli_args(['--completion', 'invalid'])
        assert exc_info.value.code != 0


class TestScrapePodmanValueFlags:
    def test_scrape_success(self, mock_run_os_cmd):
        """Cover _scrape_podman_value_flags (lines 326-338)."""
        help_text = (
            '  -e, --env stringArray   Set environment variables\n'
            '      --rm                Remove container after exit\n'
            '  -v, --volume strings    Bind mount a volume\n'
            '      --name string       Assign a name to the container\n'
        )
        mock_run_os_cmd.set_return(stdout=help_text)
        result = _scrape_podman_value_flags()
        assert '--env' in result
        assert '-e' in result
        assert '--volume' in result
        assert '-v' in result
        assert '--name' in result
        # --rm has no value type, should not be included
        assert '--rm' not in result

    def test_scrape_failure(self, mock_run_os_cmd):
        """Cover _scrape_podman_value_flags returning None (lines 327-328)."""
        mock_run_os_cmd.set_return(returncode=1)
        assert _scrape_podman_value_flags() is None


class TestCheckFlags:
    def test_version_failure(self, mock_run_os_cmd, capsys):
        """Cover check_flags when podman --version fails (lines 344-347)."""
        mock_run_os_cmd.set_return(returncode=1)
        with pytest.raises(SystemExit) as exc_info:
            check_flags()
        assert exc_info.value.code == 1
        assert 'podman --version' in capsys.readouterr().err

    def test_scrape_failure(self, mock_run_os_cmd, capsys):
        """Cover check_flags when scrape fails (lines 351-353)."""
        mock_run_os_cmd.set_side_effect(
            [
                subprocess.CompletedProcess(
                    args='', returncode=0, stdout='podman 4.5.0\n', stderr=''
                ),
                subprocess.CompletedProcess(args='', returncode=1, stdout='', stderr=''),
            ]
        )
        with pytest.raises(SystemExit) as exc_info:
            check_flags()
        assert exc_info.value.code == 1
        assert 'scrape' in capsys.readouterr().err

    def test_sets_match(self, mock_run_os_cmd, capsys):
        """Cover check_flags when sets match (lines 362-364)."""
        # Build help text matching _scrape_podman_value_flags regex:
        #   \s*(?P<short>-\w)?,?\s*(?P<long>--[^\s]+)\s+(?P<val_type>[^\s]+)?\s{2,}(?P<help>\w+.*)
        # Short flags must appear on the same line as a --long flag to be scraped.
        # Map known short<->long pairs from the frozenset.
        known_pairs = {
            '-a': '--attach',
            '-c': '--cpu-shares',
            '-e': '--env',
            '-h': '--hostname',
            '-l': '--label',
            '-m': '--memory',
            '-p': '--publish',
            '-u': '--user',
            '-v': '--volume',
            '-w': '--workdir',
        }
        lines = []
        paired_longs = set(known_pairs.values())
        # Emit paired lines (short + long together)
        for short, long in sorted(known_pairs.items()):
            lines.append(f'  {short}, {long} string    Description for {long}')
        # Emit remaining long-only flags
        for flag in sorted(PODMAN_RUN_VALUE_FLAGS):
            if flag.startswith('--') and flag not in paired_longs:
                lines.append(f'      {flag} string    Description for {flag}')
        help_text = '\n'.join(lines) + '\n'

        mock_run_os_cmd.set_side_effect(
            [
                subprocess.CompletedProcess(
                    args='', returncode=0, stdout='podman 4.5.0\n', stderr=''
                ),
                subprocess.CompletedProcess(args='', returncode=0, stdout=help_text, stderr=''),
            ]
        )
        with pytest.raises(SystemExit) as exc_info:
            check_flags()
        assert exc_info.value.code == 0
        assert 'match' in capsys.readouterr().out.lower()

    def test_added_and_removed(self, mock_run_os_cmd, capsys):
        """Cover check_flags with diffs (lines 366-375)."""
        # Return help with one extra flag and none of the static flags
        help_text = '      --new-flag string  New flag\n'
        mock_run_os_cmd.set_side_effect(
            [
                subprocess.CompletedProcess(
                    args='', returncode=0, stdout='podman 4.5.0\n', stderr=''
                ),
                subprocess.CompletedProcess(args='', returncode=0, stdout=help_text, stderr=''),
            ]
        )
        with pytest.raises(SystemExit) as exc_info:
            check_flags()
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert 'Missing' in out or 'add' in out
        assert 'Extra' in out or 'removed' in out


class TestPodrunParser:
    """Verify _PodrunParser classmethod accessors track flags from the parsers."""

    def _ensure_run_parser(self, mock_run_os_cmd):
        """Build the run parser so the registry is populated."""
        try:
            parse_cli_args(['--help'])
        except SystemExit:
            pass

    def test_registry_tracks_flags(self, mock_run_os_cmd):
        self._ensure_run_parser(mock_run_os_cmd)
        flags = _PodrunParser.get_flags()
        assert '--user-overlay' in flags
        assert '--host-overlay' in flags
        assert '--interactive-overlay' in flags
        assert '--workspace' in flags
        assert '--adhoc' in flags
        assert '--name' in flags
        assert '--version' in flags

    def test_registry_tracks_value_flags(self, mock_run_os_cmd):
        self._ensure_run_parser(mock_run_os_cmd)
        value_flags = _PodrunParser.get_value_flags()
        # Value flags (nargs != 0)
        assert '--name' in value_flags
        assert '--shell' in value_flags
        assert '--completion' in value_flags
        assert '--config' in value_flags
        # Boolean flags should NOT be in value_flags
        assert '--user-overlay' not in value_flags
        assert '--host-overlay' not in value_flags
        assert '--print-cmd' not in value_flags

    def test_registry_tracks_store_subcommands(self, mock_run_os_cmd):
        _build_parser()
        subcmds = _PodrunParser.get_subcommands('store')
        assert 'init' in subcmds
        assert 'destroy' in subcmds
        assert 'info' in subcmds

    def test_subparser_flags_tracked(self, mock_run_os_cmd):
        _build_parser()
        init_flags = _PodrunParser.get_flags('store init')
        assert '--store-dir' in init_flags
        assert '--registry' in init_flags
        assert '--storage-driver' in init_flags

    def test_top_level_subcommands(self, mock_run_os_cmd):
        _build_parser()
        top = _PodrunParser.top_level_subcommands()
        assert 'store' in top

    def test_nested_subcommand_flags(self, mock_run_os_cmd):
        _build_parser()
        nested = _PodrunParser.nested_subcommand_flags()
        assert 'store' in nested
        assert 'init' in nested['store']
        assert '--store-dir' in nested['store']['init']

    def test_completion_contains_registry_flags(self, mock_run_os_cmd, capsys):
        """Completion output contains all flags from the registry."""
        with pytest.raises(SystemExit):
            parse_cli_args(['--completion', 'bash'])
        out = capsys.readouterr().out
        for flag in _PodrunParser.get_flags():
            assert flag in out, f'{flag} missing from bash completion'

    def test_completion_contains_store_subcommands(self, mock_run_os_cmd, capsys):
        """Completion output contains store subcommands."""
        with pytest.raises(SystemExit):
            parse_cli_args(['--completion', 'bash'])
        out = capsys.readouterr().out
        assert 'store' in out
        assert 'init' in out
        assert 'destroy' in out
        assert 'info' in out

    def test_mutually_exclusive_group_tracks_flags(self, mock_run_os_cmd):
        """Flags added via add_mutually_exclusive_group appear in the registry."""
        _build_parser()
        flags = _PodrunParser.get_flags()
        assert '--login' in flags
        assert '--no-login' in flags
        assert '--workspace' in flags
        assert '--adhoc' in flags

    def test_mutually_exclusive_group_value_flags(self, mock_run_os_cmd):
        """Value-taking args in a mutually exclusive group are tracked as value_flags."""
        parser = _PodrunParser(cmd_path='_mextest', prog='test', description='desc')
        group = parser.add_mutually_exclusive_group()
        group.add_argument('--opt-a', metavar='VAL')
        group.add_argument('--opt-b', action='store_const', const=True, default=None)
        flags = _PodrunParser.get_flags('_mextest')
        assert '--opt-a' in flags
        assert '--opt-b' in flags
        value_flags = _PodrunParser.get_value_flags('_mextest')
        assert '--opt-a' in value_flags
        assert '--opt-b' not in value_flags

    def test_format_help_proxy(self, mock_run_os_cmd):
        """_PodrunParser.format_help() proxies to the inner parser."""
        parser = _PodrunParser(cmd_path='_test', prog='test', description='desc')
        help_text = parser.format_help()
        assert 'desc' in help_text
