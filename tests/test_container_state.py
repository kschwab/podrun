from unittest.mock import patch


from podrun.podrun import Config, detect_container_state, handle_container_state, yes_no_prompt


class TestDetectContainerState:
    def test_no_name(self, mock_run_os_cmd):
        assert detect_container_state('') is None
        assert detect_container_state(None) is None
        assert len(mock_run_os_cmd.calls) == 0

    def test_unknown_status(self, mock_run_os_cmd):
        mock_run_os_cmd.set_return(stdout='configuring\n')
        assert detect_container_state('test') is None

    def test_global_flags_in_inspect(self, mock_run_os_cmd):
        mock_run_os_cmd.set_return(stdout='running\n')
        detect_container_state('test', global_flags=['--root=/store', '--runroot=/run'])
        assert '--root=/store' in mock_run_os_cmd.calls[0]
        assert '--runroot=/run' in mock_run_os_cmd.calls[0]
        assert 'inspect' in mock_run_os_cmd.calls[0]

    def test_no_global_flags_by_default(self, mock_run_os_cmd):
        mock_run_os_cmd.set_return(stdout='running\n')
        detect_container_state('test')
        cmd = mock_run_os_cmd.calls[0]
        assert cmd.startswith('podman inspect')


class TestYesNoPrompt:
    def test_non_interactive_defaults_yes(self, capsys):
        result = yes_no_prompt('Continue?', True, False)
        assert result is True
        err = capsys.readouterr().err
        assert 'yes' in err

    def test_non_interactive_defaults_no(self, capsys):
        result = yes_no_prompt('Continue?', False, False)
        assert result is False
        err = capsys.readouterr().err
        assert 'no' in err

    def test_interactive_yes(self):
        with patch('builtins.input', return_value='y'):
            result = yes_no_prompt('Continue?', False, True)
        assert result is True

    def test_interactive_no(self):
        with patch('builtins.input', return_value='n'):
            result = yes_no_prompt('Continue?', True, True)
        assert result is False

    def test_empty_input_uses_default(self):
        with patch('builtins.input', return_value=''):
            result = yes_no_prompt('Continue?', True, True)
        assert result is True

    def test_invalid_then_valid_input(self):
        """Cover the retry loop for invalid input (lines 237-238)."""
        with patch('builtins.input', side_effect=['maybe', 'y']):
            result = yes_no_prompt('Continue?', False, True)
        assert result is True


class TestHandleContainerState:
    def test_no_name_returns_run(self, make_config, mock_run_os_cmd):
        config = make_config(name=None)
        assert handle_container_state(config) == 'run'

    def test_running_both_false_returns_none(self, make_config, mock_run_os_cmd):
        mock_run_os_cmd.set_return(stdout='running\n')
        config = make_config(name='test', auto_attach=False, auto_replace=False)
        assert handle_container_state(config) is None

    def test_running_prompt_attach_yes(self, mock_run_os_cmd):
        """Running container, unset auto flags, user says yes to attach (lines 703-706).

        auto_attach=None bypasses the 'is False' guard to reach the prompt path.
        """
        mock_run_os_cmd.set_return(stdout='running\n')
        config = Config(image='alpine', name='test', auto_attach=None, auto_replace=None)
        with patch('podrun.podrun.sys.stdin') as mock_stdin, patch(
            'builtins.input', return_value='y'
        ):
            mock_stdin.isatty.return_value = True
            result = handle_container_state(config)
        assert result == 'attach'

    def test_running_prompt_replace_yes(self, mock_run_os_cmd):
        """Running container, user declines attach but accepts replace (lines 707-710)."""
        mock_run_os_cmd.set_return(stdout='running\n')
        config = Config(image='alpine', name='test', auto_attach=None, auto_replace=None)
        with patch('podrun.podrun.sys.stdin') as mock_stdin, patch(
            'builtins.input', side_effect=['n', 'y']
        ):
            mock_stdin.isatty.return_value = True
            result = handle_container_state(config)
        assert result == 'replace'

    def test_running_prompt_both_no(self, mock_run_os_cmd):
        """Running container, user declines both attach and replace (line 711)."""
        mock_run_os_cmd.set_return(stdout='running\n')
        config = Config(image='alpine', name='test', auto_attach=None, auto_replace=None)
        with patch('podrun.podrun.sys.stdin') as mock_stdin, patch(
            'builtins.input', side_effect=['n', 'n']
        ):
            mock_stdin.isatty.return_value = True
            result = handle_container_state(config)
        assert result is None

    def test_stopped_interactive_replace_yes(self, mock_run_os_cmd):
        """Stopped container, interactive, user accepts replace."""
        mock_run_os_cmd.set_return(stdout='exited\n')
        config = Config(image='alpine', name='test')
        with patch('podrun.podrun.sys.stdin') as mock_stdin, patch(
            'builtins.input', return_value='y'
        ):
            mock_stdin.isatty.return_value = True
            result = handle_container_state(config)
        assert result == 'replace'

    def test_stopped_interactive_replace_no(self, mock_run_os_cmd):
        """Stopped container, interactive, user declines replace."""
        mock_run_os_cmd.set_return(stdout='exited\n')
        config = Config(image='alpine', name='test')
        with patch('podrun.podrun.sys.stdin') as mock_stdin, patch(
            'builtins.input', return_value='n'
        ):
            mock_stdin.isatty.return_value = True
            result = handle_container_state(config)
        assert result is None

    def test_stopped_non_interactive_returns_none(self, mock_run_os_cmd):
        """Stopped container, non-interactive, no auto flags -> None."""
        mock_run_os_cmd.set_return(stdout='exited\n')
        config = Config(image='alpine', name='test')
        with patch('podrun.podrun.sys.stdin') as mock_stdin:
            mock_stdin.isatty.return_value = False
            result = handle_container_state(config)
        assert result is None


class TestAutoFlagPrecedence:
    """Direct handle_container_state() tests with explicit auto flags.

    No prompting or stdin mocking needed — auto flags bypass interactive paths.
    """

    def test_running_auto_attach(self, mock_run_os_cmd):
        """Running + auto_attach=True → 'attach'."""
        mock_run_os_cmd.set_return(stdout='running\n')
        config = Config(image='alpine', name='test', auto_attach=True)
        assert handle_container_state(config) == 'attach'

    def test_running_auto_replace(self, mock_run_os_cmd):
        """Running + auto_replace=True → 'replace'."""
        mock_run_os_cmd.set_return(stdout='running\n')
        config = Config(image='alpine', name='test', auto_replace=True)
        assert handle_container_state(config) == 'replace'

    def test_running_both_true_attach_wins(self, mock_run_os_cmd):
        """Running + both True → 'attach' (auto_attach takes precedence)."""
        mock_run_os_cmd.set_return(stdout='running\n')
        config = Config(image='alpine', name='test', auto_attach=True, auto_replace=True)
        assert handle_container_state(config) == 'attach'

    def test_running_both_false_returns_none(self, mock_run_os_cmd):
        """Running + both False → None (no action)."""
        mock_run_os_cmd.set_return(stdout='running\n')
        config = Config(image='alpine', name='test', auto_attach=False, auto_replace=False)
        assert handle_container_state(config) is None

    def test_stopped_auto_attach_warns_returns_none(self, mock_run_os_cmd, capsys):
        """Stopped + auto_attach=True → warns and returns None (can't attach to stopped)."""
        mock_run_os_cmd.set_return(stdout='exited\n')
        config = Config(image='alpine', name='test', auto_attach=True)
        assert handle_container_state(config) is None
        assert 'cannot auto-attach to container' in capsys.readouterr().err.lower()

    def test_stopped_auto_replace(self, mock_run_os_cmd):
        """Stopped + auto_replace=True → 'replace'."""
        mock_run_os_cmd.set_return(stdout='exited\n')
        config = Config(image='alpine', name='test', auto_replace=True)
        assert handle_container_state(config) == 'replace'

    def test_stopped_both_true_replace_wins(self, mock_run_os_cmd, capsys):
        """Stopped + both True → 'replace' (can't attach to stopped, falls through to replace)."""
        mock_run_os_cmd.set_return(stdout='exited\n')
        config = Config(image='alpine', name='test', auto_attach=True, auto_replace=True)
        assert handle_container_state(config) == 'replace'
        assert 'cannot auto-attach to container' in capsys.readouterr().err.lower()

    def test_none_both_true_returns_run(self, mock_run_os_cmd):
        """No container (inspect fails) + both True → 'run'."""
        mock_run_os_cmd.set_return(returncode=1)
        config = Config(image='alpine', name='test', auto_attach=True, auto_replace=True)
        assert handle_container_state(config) == 'run'
