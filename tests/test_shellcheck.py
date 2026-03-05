import shutil
import subprocess

import pytest

from podrun.podrun import (
    generate_run_entrypoint,
    generate_rc_sh,
    _generate_bash_completion,
    _build_parser,
)

pytestmark = pytest.mark.skipif(
    shutil.which('shellcheck') is None,
    reason='shellcheck not available',
)


SHELLCHECK_DIALECTS = ('sh', 'bash', 'dash', 'ksh')


def _run_shellcheck(path, shell=None):
    cmd = ['shellcheck', '-S', 'warning']
    if shell:
        cmd.extend(['--shell', shell])
    cmd.append(path)
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result


class TestShellcheckEntrypoint:
    @pytest.mark.parametrize('dialect', SHELLCHECK_DIALECTS)
    def test_default_shell(self, dialect, make_config, podrun_tmp):
        config = make_config(user_overlay=True, shell=None, login=False)
        path = generate_run_entrypoint(config)
        result = _run_shellcheck(path, shell=dialect)
        assert result.returncode == 0, f'shellcheck ({dialect}) errors:\n{result.stdout}'

    @pytest.mark.parametrize('dialect', SHELLCHECK_DIALECTS)
    def test_custom_shell(self, dialect, make_config, podrun_tmp):
        config = make_config(user_overlay=True, shell='zsh', login=False)
        path = generate_run_entrypoint(config)
        result = _run_shellcheck(path, shell=dialect)
        assert result.returncode == 0, f'shellcheck ({dialect}) errors:\n{result.stdout}'

    @pytest.mark.parametrize('dialect', SHELLCHECK_DIALECTS)
    def test_login_shell(self, dialect, make_config, podrun_tmp):
        config = make_config(user_overlay=True, shell=None, login=True)
        path = generate_run_entrypoint(config)
        result = _run_shellcheck(path, shell=dialect)
        assert result.returncode == 0, f'shellcheck ({dialect}) errors:\n{result.stdout}'

    @pytest.mark.parametrize('dialect', SHELLCHECK_DIALECTS)
    def test_custom_caps(self, dialect, make_config, podrun_tmp):
        config = make_config(
            user_overlay=True,
            bootstrap_caps=['CAP_DAC_OVERRIDE'],
        )
        path = generate_run_entrypoint(config)
        result = _run_shellcheck(path, shell=dialect)
        assert result.returncode == 0, f'shellcheck ({dialect}) errors:\n{result.stdout}'


class TestShellcheckRcSh:
    @pytest.mark.parametrize('dialect', SHELLCHECK_DIALECTS)
    def test_rc_sh(self, dialect, make_config, podrun_tmp, mock_run_os_cmd):
        mock_run_os_cmd.set_return(stdout='Intel Core i7 (4 vCPU)\n')
        config = make_config(prompt_banner='test')
        path = generate_rc_sh(config)
        result = _run_shellcheck(path, shell=dialect)
        assert result.returncode == 0, f'shellcheck ({dialect}) errors:\n{result.stdout}'


class TestShellcheckCompletion:
    def test_bash_completion(self, tmp_path, mock_run_os_cmd):
        # Ensure parsers are built so _PodrunParser._registry is populated
        _build_parser()
        script = _generate_bash_completion()
        path = tmp_path / 'completion.bash'
        path.write_text(script)
        result = _run_shellcheck(str(path))
        assert result.returncode == 0, f'shellcheck errors:\n{result.stdout}'
