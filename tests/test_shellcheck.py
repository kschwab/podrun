import shutil
import subprocess

import pytest

from podrun.podrun import (
    generate_run_entrypoint,
    generate_rc_sh,
    _generate_bash_completion,
    _generate_store_activate,
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


class TestShellcheckActivate:
    @pytest.mark.parametrize('dialect', SHELLCHECK_DIALECTS)
    def test_activate_without_registry(self, dialect, tmp_path):
        store_dir = tmp_path / 'store'
        store_dir.mkdir()
        bin_dir = store_dir / 'bin'
        bin_dir.mkdir()
        _generate_store_activate(store_dir, bin_dir, '/tmp/podrun-stores/abc123')
        result = _run_shellcheck(str(store_dir / 'activate'), shell=dialect)
        assert result.returncode == 0, f'shellcheck ({dialect}) errors:\n{result.stdout}'

    @pytest.mark.parametrize('dialect', SHELLCHECK_DIALECTS)
    def test_activate_with_registry(self, dialect, tmp_path):
        store_dir = tmp_path / 'store'
        store_dir.mkdir()
        bin_dir = store_dir / 'bin'
        bin_dir.mkdir()
        reg_conf = str(store_dir / 'registries.conf')
        (store_dir / 'registries.conf').write_text('# registry config\n')
        _generate_store_activate(
            store_dir, bin_dir, '/tmp/podrun-stores/abc123', registries_conf=reg_conf
        )
        result = _run_shellcheck(str(store_dir / 'activate'), shell=dialect)
        assert result.returncode == 0, f'shellcheck ({dialect}) errors:\n{result.stdout}'


class TestShellcheckWrapperScripts:
    @pytest.mark.parametrize('dialect', SHELLCHECK_DIALECTS)
    def test_podman_wrapper(self, dialect, tmp_path):
        wrapper = tmp_path / 'podman'
        wrapper.write_text(
            '#!/bin/sh\nexec /usr/bin/podman'
            ' --root /store/graphroot'
            ' --runroot /store/runroot'
            ' --storage-driver overlay "$@"\n'
        )
        result = _run_shellcheck(str(wrapper), shell=dialect)
        assert result.returncode == 0, f'shellcheck ({dialect}) errors:\n{result.stdout}'

    @pytest.mark.parametrize('dialect', SHELLCHECK_DIALECTS)
    def test_podrun_wrapper(self, dialect, tmp_path):
        wrapper = tmp_path / 'podrun'
        wrapper.write_text(
            '#!/bin/sh\nexec /usr/bin/python3 -m podrun'
            ' --root /store/graphroot'
            ' --runroot /store/runroot'
            ' --storage-driver overlay "$@"\n'
        )
        result = _run_shellcheck(str(wrapper), shell=dialect)
        assert result.returncode == 0, f'shellcheck ({dialect}) errors:\n{result.stdout}'
