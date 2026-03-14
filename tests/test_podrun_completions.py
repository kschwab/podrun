import pytest

from podrun.podrun import (
    _completion_data,
    _generate_bash_completion,
    _generate_fish_completion,
    _generate_zsh_completion,
    main,
    print_completion,
)


# ---------------------------------------------------------------------------
# _completion_data() tests
# ---------------------------------------------------------------------------


class TestCompletionData:
    def test_returns_dict(self):
        cd = _completion_data()
        assert isinstance(cd, dict)
        assert 'flags_str' in cd
        assert 'value_flags_str' in cd
        assert 'subcmds_str' in cd

    def test_flags_contain_root_flags(self):
        cd = _completion_data()
        flags = cd['flags_str'].split()
        for f in [
            '--print-cmd',
            '--dry-run',
            '--config',
            '--no-devconfig',
            '--completion',
            '--local-store',
            '--local-store-ignore',
            '--local-store-auto-init',
            '--local-store-info',
            '--local-store-destroy',
            '--config-script',
        ]:
            assert f in flags, f'{f} not found in flags_str'

    def test_flags_contain_run_flags(self):
        cd = _completion_data()
        flags = cd['flags_str'].split()
        for f in [
            '--name',
            '--label',
            '-l',
            '--shell',
            '--user-overlay',
            '--host-overlay',
            '--interactive-overlay',
            '--workspace',
            '--adhoc',
            '--export',
            '--x11',
            '--podman-remote',
            '--print-overlays',
            '--login',
            '--no-login',
            '--prompt-banner',
            '--auto-attach',
            '--auto-replace',
            '--fuse-overlayfs',
            '--dot-files-overlay',
            '--dotfiles',
        ]:
            assert f in flags, f'{f} not found in flags_str'

    def test_flags_exclude_passthrough(self):
        """Podman passthrough flags should NOT appear in podrun flags."""
        cd = _completion_data()
        flags = cd['flags_str'].split()
        for f in [
            '--env',
            '-e',
            '--volume',
            '--memory',
            '--user',
            '--workdir',
            '--publish',
            '--hostname',
            '--network',
            '--mount',
            '--cpus',
            '--cap-add',
            '--entrypoint',
            '--userns',
            '--annotation',
            '--security-opt',
        ]:
            assert f not in flags, f'{f} should not be in flags_str'

    def test_value_flags_contain_value_taking(self):
        cd = _completion_data()
        vflags = cd['value_flags_str'].split()
        for f in [
            '--config',
            '--config-script',
            '--name',
            '--shell',
            '--completion',
            '--export',
            '--prompt-banner',
            '--local-store',
            '--label',
            '-l',
        ]:
            assert f in vflags, f'{f} not found in value_flags_str'

    def test_value_flags_exclude_boolean(self):
        cd = _completion_data()
        vflags = cd['value_flags_str'].split()
        for f in [
            '--print-cmd',
            '--dry-run',
            '--no-devconfig',
            '--user-overlay',
            '--host-overlay',
            '--workspace',
            '--adhoc',
            '--x11',
            '--podman-remote',
            '--print-overlays',
            '--auto-attach',
            '--auto-replace',
            '--fuse-overlayfs',
            '--local-store-ignore',
            '--local-store-auto-init',
            '--local-store-info',
            '--local-store-destroy',
            '--interactive-overlay',
            '--dot-files-overlay',
            '--dotfiles',
        ]:
            assert f not in vflags, f'{f} should not be in value_flags_str'

    def test_subcmds_str_empty(self):
        cd = _completion_data()
        assert cd['subcmds_str'] == ''

    def test_version_flag_in_flags(self):
        cd = _completion_data()
        flags = cd['flags_str'].split()
        assert '--version' in flags
        assert '-v' in flags

    def test_login_flags_classified_correctly(self):
        """--login and --no-login are store_const, should not be value flags."""
        cd = _completion_data()
        vflags = cd['value_flags_str'].split()
        assert '--login' not in vflags
        assert '--no-login' not in vflags


# ---------------------------------------------------------------------------
# Bash completion generator tests
# ---------------------------------------------------------------------------


class TestBashCompletion:
    def test_output_nonempty(self):
        out = _generate_bash_completion()
        assert len(out) > 0

    def test_contains_function_name(self):
        out = _generate_bash_completion()
        assert '_podrun()' in out

    def test_contains_podman_complete(self):
        out = _generate_bash_completion()
        assert 'podman __completeNoDesc' in out

    def test_contains_podrun_flags(self):
        cd = _completion_data()
        out = _generate_bash_completion()
        assert cd['flags_str'] in out

    def test_contains_complete_registration(self):
        out = _generate_bash_completion()
        assert 'complete -o default -F _podrun podrun' in out

    def test_contains_flag_stripping_logic(self):
        out = _generate_bash_completion()
        assert 'is_podrun=true' in out

    def test_contains_run_injection(self):
        out = _generate_bash_completion()
        assert 'args=("run"' in out

    def test_contains_cobra_directives(self):
        out = _generate_bash_completion()
        assert 'directive' in out
        assert 'nospace' in out

    def test_no_subcmd_context(self):
        """Should not contain subcommand context detection blocks."""
        out = _generate_bash_completion()
        assert 'podrun_subcommands' not in out
        assert 'podrun_subcmd' not in out


# ---------------------------------------------------------------------------
# Zsh completion generator tests
# ---------------------------------------------------------------------------


class TestZshCompletion:
    def test_output_nonempty(self):
        out = _generate_zsh_completion()
        assert len(out) > 0

    def test_contains_compdef_header(self):
        out = _generate_zsh_completion()
        assert '#compdef podrun' in out

    def test_contains_function_name(self):
        out = _generate_zsh_completion()
        assert '_podrun()' in out

    def test_contains_podman_complete(self):
        out = _generate_zsh_completion()
        assert 'podman __complete' in out

    def test_contains_podrun_flags(self):
        cd = _completion_data()
        out = _generate_zsh_completion()
        assert cd['flags_str'] in out

    def test_contains_compdef_registration(self):
        out = _generate_zsh_completion()
        assert 'compdef _podrun podrun' in out

    def test_contains_describe(self):
        out = _generate_zsh_completion()
        assert '_describe' in out

    def test_contains_run_injection(self):
        out = _generate_zsh_completion()
        assert 'args=("run"' in out

    def test_no_subcmd_context(self):
        out = _generate_zsh_completion()
        assert 'podrun_subcommands' not in out
        assert 'podrun_subcmd' not in out


# ---------------------------------------------------------------------------
# Fish completion generator tests
# ---------------------------------------------------------------------------


class TestFishCompletion:
    def test_output_nonempty(self):
        out = _generate_fish_completion()
        assert len(out) > 0

    def test_contains_function_name(self):
        out = _generate_fish_completion()
        assert '__podrun_complete' in out

    def test_contains_podman_complete(self):
        out = _generate_fish_completion()
        assert 'podman __complete' in out

    def test_contains_podrun_flags(self):
        cd = _completion_data()
        out = _generate_fish_completion()
        assert cd['flags_str'] in out

    def test_contains_complete_registration(self):
        out = _generate_fish_completion()
        assert "complete -c podrun -f -a '(__podrun_complete)'" in out

    def test_contains_run_injection(self):
        out = _generate_fish_completion()
        assert 'set args run' in out

    def test_no_subcmd_context(self):
        out = _generate_fish_completion()
        assert 'podrun_subcommands' not in out
        assert 'podrun_subcmd' not in out


# ---------------------------------------------------------------------------
# print_completion() integration tests
# ---------------------------------------------------------------------------


class TestPrintCompletion:
    def test_bash_exits_zero(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            print_completion('bash')
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert '_podrun()' in out

    def test_zsh_exits_zero(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            print_completion('zsh')
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert '#compdef podrun' in out

    def test_fish_exits_zero(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            print_completion('fish')
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert '__podrun_complete' in out


# ---------------------------------------------------------------------------
# main() integration tests
# ---------------------------------------------------------------------------


class TestMainCompletion:
    def test_main_completion_bash(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(['--completion', 'bash'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert '_podrun()' in out

    def test_main_completion_zsh(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(['--completion', 'zsh'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert '#compdef podrun' in out

    def test_main_completion_fish(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(['--completion', 'fish'])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert '__podrun_complete' in out
