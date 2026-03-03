import pytest

import podrun.podrun as podrun_mod
from podrun.podrun import _devcontainer_run_args, _resolve_podman_path, merge_config


class TestMergeConfigPrecedence:
    """CLI > podrun_cfg > devcontainer for all applicable fields."""

    def test_name_cli_wins(self, make_cli_args):
        cli = make_cli_args(name='cli-name')
        podrun_cfg = {'name': 'cfg-name'}
        dc = {'image': 'alpine'}
        config = merge_config(cli, podrun_cfg, dc)
        assert config.name == 'cli-name'

    def test_name_podrun_cfg_over_derived(self, make_cli_args):
        cli = make_cli_args(name=None)
        podrun_cfg = {'name': 'cfg-name'}
        dc = {'image': 'alpine'}
        config = merge_config(cli, podrun_cfg, dc)
        assert config.name == 'cfg-name'

    def test_shell_cli_wins(self, make_cli_args):
        cli = make_cli_args(shell='zsh')
        podrun_cfg = {'shell': 'fish'}
        dc = {'image': 'alpine'}
        config = merge_config(cli, podrun_cfg, dc)
        assert config.shell == 'zsh'

    def test_prompt_banner_cli_wins(self, make_cli_args):
        cli = make_cli_args(prompt_banner='cli-banner')
        podrun_cfg = {'promptBanner': 'cfg-banner'}
        dc = {'image': 'alpine'}
        config = merge_config(cli, podrun_cfg, dc)
        assert config.prompt_banner == 'cli-banner'

    def test_prompt_banner_falls_back_to_image(self, make_cli_args):
        cli = make_cli_args(prompt_banner=None)
        config = merge_config(cli, {}, {'image': 'myimage'})
        assert config.prompt_banner == 'myimage'


class TestOverlayImplication:
    def test_workspace_implies_host_and_interactive(self, make_cli_args):
        cli = make_cli_args(workspace=True)
        config = merge_config(cli, {}, {'image': 'alpine'})
        assert config.workspace is True
        assert config.host_overlay is True
        assert config.interactive_overlay is True
        assert config.user_overlay is True

    def test_adhoc_implies_workspace_and_overlays(self, make_cli_args):
        cli = make_cli_args(adhoc=True)
        config = merge_config(cli, {}, {'image': 'alpine'})
        assert config.adhoc is True
        assert config.workspace is True
        assert config.host_overlay is True
        assert config.interactive_overlay is True
        assert config.user_overlay is True

    def test_adhoc_from_podrun_cfg(self, make_cli_args):
        cli = make_cli_args()
        config = merge_config(cli, {'adhoc': True}, {'image': 'alpine'})
        assert config.adhoc is True
        assert config.workspace is True
        assert config.host_overlay is True
        assert config.interactive_overlay is True
        assert config.user_overlay is True

    def test_host_implies_user(self, make_cli_args):
        cli = make_cli_args(host_overlay=True)
        config = merge_config(cli, {}, {'image': 'alpine'})
        assert config.user_overlay is True
        assert config.interactive_overlay is False

    def test_interactive_alone(self, make_cli_args):
        cli = make_cli_args(interactive_overlay=True)
        config = merge_config(cli, {}, {'image': 'alpine'})
        assert config.interactive_overlay is True
        assert config.user_overlay is False
        assert config.host_overlay is False

    def test_user_alone(self, make_cli_args):
        cli = make_cli_args(user_overlay=True)
        config = merge_config(cli, {}, {'image': 'alpine'})
        assert config.user_overlay is True
        assert config.host_overlay is False
        assert config.interactive_overlay is False


class TestNameDerivation:
    def test_derived_from_image_basename(self, make_cli_args):
        cli = make_cli_args()
        config = merge_config(cli, {}, {'image': 'registry.io/org/my-image:v1.0'})
        assert config.name == 'my-image-v1.0'

    def test_special_chars_replaced(self, make_cli_args):
        cli = make_cli_args()
        config = merge_config(cli, {}, {'image': 'user/img@sha256:abc'})
        assert config.name == 'img-sha256-abc'


class TestImageSource:
    def test_image_from_devcontainer(self, make_cli_args):
        cli = make_cli_args()
        config = merge_config(cli, {}, {'image': 'dc-image'})
        assert config.image == 'dc-image'

    def test_image_from_trailing_args(self, make_cli_args):
        cli = make_cli_args(trailing_args=['my-image', 'cmd1', 'cmd2'])
        config = merge_config(cli, {}, {})
        assert config.image == 'my-image'
        assert config.command == ['cmd1', 'cmd2']

    def test_explicit_command_merged(self, make_cli_args):
        cli = make_cli_args(
            trailing_args=['my-image'],
            explicit_command=['bash', '-c', 'echo hi'],
        )
        config = merge_config(cli, {}, {})
        assert config.image == 'my-image'
        assert config.command == ['bash', '-c', 'echo hi']


class TestEnvAndArgs:
    def test_container_env(self, make_cli_args):
        cli = make_cli_args()
        dc = {'image': 'alpine', 'containerEnv': {'FOO': 'bar'}}
        config = merge_config(cli, {}, dc)
        assert config.container_env == {'FOO': 'bar'}

    def test_remote_env(self, make_cli_args):
        cli = make_cli_args()
        dc = {'image': 'alpine', 'remoteEnv': {'BAZ': 'qux'}}
        config = merge_config(cli, {}, dc)
        assert config.remote_env == {'BAZ': 'qux'}

    def test_podman_args(self, make_cli_args):
        cli = make_cli_args()
        podrun_cfg = {'podmanArgs': ['--security-opt=label=disable']}
        config = merge_config(cli, podrun_cfg, {'image': 'alpine'})
        assert '--security-opt=label=disable' in config.podman_args

    def test_passthrough_args(self, make_cli_args):
        cli = make_cli_args(passthrough_args=['--rm', '--privileged'])
        config = merge_config(cli, {}, {'image': 'alpine'})
        assert config.passthrough_args == ['--rm', '--privileged']

    def test_bootstrap_caps_dedup(self, make_cli_args):
        cli = make_cli_args()
        podrun_cfg = {'podmanArgs': ['--cap-add=CAP_DAC_OVERRIDE']}
        config = merge_config(cli, podrun_cfg, {'image': 'alpine'})
        assert 'CAP_DAC_OVERRIDE' not in config.bootstrap_caps
        assert 'CAP_CHOWN' in config.bootstrap_caps
        assert 'CAP_FOWNER' in config.bootstrap_caps
        assert 'CAP_SETPCAP' in config.bootstrap_caps


class TestConfigScript:
    def test_config_script_prepended(self, make_cli_args, mock_run_os_cmd):
        mock_run_os_cmd.set_return(stdout='--rm --privileged')
        cli = make_cli_args(had_config_script=False)
        podrun_cfg = {'configScript': '/path/to/script', 'podmanArgs': ['--init']}
        config = merge_config(cli, podrun_cfg, {'image': 'alpine'})
        assert config.podman_args == ['--rm', '--privileged', '--init']

    def test_config_script_skipped_when_cli_had_config_script(self, make_cli_args, mock_run_os_cmd):
        cli = make_cli_args(had_config_script=True)
        podrun_cfg = {'configScript': '/path/to/script', 'podmanArgs': ['--init']}
        config = merge_config(cli, podrun_cfg, {'image': 'alpine'})
        assert config.podman_args == ['--init']
        assert len(mock_run_os_cmd.calls) == 0

    def test_config_script_failure_warns(self, make_cli_args, mock_run_os_cmd, capsys):
        mock_run_os_cmd.set_return(returncode=1, stderr='boom')
        cli = make_cli_args(had_config_script=False)
        podrun_cfg = {'configScript': '/bad/script', 'podmanArgs': []}
        merge_config(cli, podrun_cfg, {'image': 'alpine'})
        captured = capsys.readouterr()
        assert 'Warning' in captured.err
        assert '/bad/script' in captured.err


class TestImageDedup:
    """Test image deduplication when devcontainer.json image matches trailing args."""

    def test_json_image_with_matching_trailing(self, make_cli_args):
        """trailing[0] matches json image → deduped, no command."""
        cli = make_cli_args(trailing_args=['alpine'])
        config = merge_config(cli, {}, {'image': 'alpine'})
        assert config.image == 'alpine'
        assert config.command == []

    def test_json_image_with_different_trailing(self, make_cli_args):
        """trailing[0] differs from json image → all trailing becomes command."""
        cli = make_cli_args(trailing_args=['-c', 'echo hi'])
        config = merge_config(cli, {}, {'image': 'alpine'})
        assert config.image == 'alpine'
        assert config.command == ['-c', 'echo hi']

    def test_json_image_no_trailing(self, make_cli_args):
        """No trailing args → command empty."""
        cli = make_cli_args(trailing_args=[])
        config = merge_config(cli, {}, {'image': 'alpine'})
        assert config.image == 'alpine'
        assert config.command == []

    def test_no_json_image(self, make_cli_args):
        """No json image → trailing[0] is image (existing behavior)."""
        cli = make_cli_args(trailing_args=['myimage', 'cmd1'])
        config = merge_config(cli, {}, {})
        assert config.image == 'myimage'
        assert config.command == ['cmd1']

    def test_json_image_with_matching_plus_command(self, make_cli_args):
        """['alpine', '-c', 'echo hi'] with json image alpine → command is ['-c', 'echo hi']."""
        cli = make_cli_args(trailing_args=['alpine', '-c', 'echo hi'])
        config = merge_config(cli, {}, {'image': 'alpine'})
        assert config.image == 'alpine'
        assert config.command == ['-c', 'echo hi']


class TestExportsConfig:
    """Test --export merging into Config."""

    def test_exports_from_cli(self, make_cli_args):
        cli = make_cli_args(export=['/opt/sdk:./sdk'])
        config = merge_config(cli, {}, {'image': 'alpine'})
        assert config.exports == ['/opt/sdk:./sdk']

    def test_exports_from_podrun_cfg(self, make_cli_args):
        cli = make_cli_args()
        podrun_cfg = {'exports': ['/opt/sdk:./sdk']}
        config = merge_config(cli, podrun_cfg, {'image': 'alpine'})
        assert config.exports == ['/opt/sdk:./sdk']

    def test_exports_cli_appends_to_config(self, make_cli_args):
        cli = make_cli_args(export=['/usr/share/data:./data'])
        podrun_cfg = {'exports': ['/opt/sdk:./sdk']}
        config = merge_config(cli, podrun_cfg, {'image': 'alpine'})
        assert config.exports == ['/opt/sdk:./sdk', '/usr/share/data:./data']

    def test_exports_empty_by_default(self, make_cli_args):
        cli = make_cli_args()
        config = merge_config(cli, {}, {'image': 'alpine'})
        assert config.exports == []

    def test_exports_copy_only_syntax(self, make_cli_args):
        cli = make_cli_args(export=['/opt/sdk:./sdk:0'])
        config = merge_config(cli, {}, {'image': 'alpine'})
        assert config.exports == ['/opt/sdk:./sdk:0']


class TestDevcontainerRunArgs:
    """Test _devcontainer_run_args converts devcontainer.json fields to podman flags."""

    def test_empty_devcontainer(self):
        assert _devcontainer_run_args({}) == []

    def test_mount_string(self):
        dc = {'mounts': ['type=bind,source=/host,target=/cont']}
        args = _devcontainer_run_args(dc)
        assert args == ['--mount=type=bind,source=/host,target=/cont']

    def test_mount_object(self):
        dc = {'mounts': [{'type': 'bind', 'source': '/a', 'target': '/b'}]}
        args = _devcontainer_run_args(dc)
        assert args == ['--mount=type=bind,source=/a,target=/b']

    def test_mount_object_extra_keys(self):
        dc = {'mounts': [{'type': 'bind', 'source': '/a', 'target': '/b', 'readonly': 'true'}]}
        args = _devcontainer_run_args(dc)
        assert len(args) == 1
        assert args[0].startswith('--mount=')
        assert 'readonly=true' in args[0]

    def test_mount_multiple(self):
        dc = {
            'mounts': [
                'type=bind,source=/x,target=/y',
                {'type': 'volume', 'source': 'vol', 'target': '/z'},
            ]
        }
        args = _devcontainer_run_args(dc)
        assert len(args) == 2
        assert args[0] == '--mount=type=bind,source=/x,target=/y'
        assert args[1] == '--mount=type=volume,source=vol,target=/z'

    def test_cap_add_single(self):
        dc = {'capAdd': ['SYS_PTRACE']}
        args = _devcontainer_run_args(dc)
        assert args == ['--cap-add=SYS_PTRACE']

    def test_cap_add_multiple(self):
        dc = {'capAdd': ['SYS_PTRACE', 'NET_ADMIN']}
        args = _devcontainer_run_args(dc)
        assert args == ['--cap-add=SYS_PTRACE', '--cap-add=NET_ADMIN']

    def test_security_opt_single(self):
        dc = {'securityOpt': ['seccomp=unconfined']}
        args = _devcontainer_run_args(dc)
        assert args == ['--security-opt=seccomp=unconfined']

    def test_security_opt_multiple(self):
        dc = {'securityOpt': ['seccomp=unconfined', 'label=disable']}
        args = _devcontainer_run_args(dc)
        assert args == ['--security-opt=seccomp=unconfined', '--security-opt=label=disable']

    def test_privileged_true(self):
        dc = {'privileged': True}
        args = _devcontainer_run_args(dc)
        assert args == ['--privileged']

    def test_privileged_false(self):
        dc = {'privileged': False}
        args = _devcontainer_run_args(dc)
        assert args == []

    def test_init_true(self):
        dc = {'init': True}
        args = _devcontainer_run_args(dc)
        assert args == ['--init']

    def test_init_false(self):
        dc = {'init': False}
        args = _devcontainer_run_args(dc)
        assert args == []

    def test_run_args(self):
        dc = {'runArgs': ['--device-cgroup-rule=a 1:1 rwm', '--memory=4g']}
        args = _devcontainer_run_args(dc)
        assert args == ['--device-cgroup-rule=a 1:1 rwm', '--memory=4g']

    def test_combined_fields(self):
        dc = {
            'mounts': ['type=bind,source=/data,target=/data'],
            'capAdd': ['SYS_PTRACE'],
            'securityOpt': ['seccomp=unconfined'],
            'privileged': True,
            'init': True,
            'runArgs': ['--memory=4g'],
        }
        args = _devcontainer_run_args(dc)
        assert args == [
            '--mount=type=bind,source=/data,target=/data',
            '--cap-add=SYS_PTRACE',
            '--security-opt=seccomp=unconfined',
            '--privileged',
            '--init',
            '--memory=4g',
        ]

    def test_ordering_mounts_before_caps(self):
        dc = {'capAdd': ['SYS_PTRACE'], 'mounts': ['type=bind,source=/a,target=/b']}
        args = _devcontainer_run_args(dc)
        mount_idx = next(i for i, a in enumerate(args) if a.startswith('--mount='))
        cap_idx = next(i for i, a in enumerate(args) if a.startswith('--cap-add='))
        assert mount_idx < cap_idx

    def test_ordering_run_args_last(self):
        dc = {'runArgs': ['--memory=4g'], 'capAdd': ['SYS_PTRACE']}
        args = _devcontainer_run_args(dc)
        cap_idx = next(i for i, a in enumerate(args) if a.startswith('--cap-add='))
        mem_idx = next(i for i, a in enumerate(args) if a == '--memory=4g')
        assert cap_idx < mem_idx

    def test_unrelated_fields_ignored(self):
        dc = {'image': 'ubuntu', 'workspaceFolder': '/app', 'containerEnv': {'A': 'B'}}
        args = _devcontainer_run_args(dc)
        assert args == []


class TestDevcontainerPrecedence:
    """Test that podmanArgs override devcontainer top-level fields."""

    def test_podman_args_after_dc_args(self, make_cli_args):
        cli = make_cli_args()
        podrun_cfg = {'podmanArgs': ['--memory=8g']}
        dc = {'runArgs': ['--memory=4g'], 'image': 'alpine'}
        config = merge_config(cli, podrun_cfg, dc)
        mem_4g_idx = config.podman_args.index('--memory=4g')
        mem_8g_idx = config.podman_args.index('--memory=8g')
        assert mem_4g_idx < mem_8g_idx

    def test_cap_add_dedup_with_bootstrap(self, make_cli_args):
        cli = make_cli_args()
        dc = {'capAdd': ['CAP_DAC_OVERRIDE'], 'image': 'alpine'}
        config = merge_config(cli, {}, dc)
        assert '--cap-add=CAP_DAC_OVERRIDE' in config.podman_args
        assert 'CAP_DAC_OVERRIDE' not in config.bootstrap_caps

    def test_run_args_after_semantic_fields(self, make_cli_args):
        cli = make_cli_args()
        dc = {
            'capAdd': ['SYS_PTRACE'],
            'runArgs': ['--cap-add=NET_ADMIN'],
            'image': 'alpine',
        }
        config = merge_config(cli, {}, dc)
        ptrace_idx = config.podman_args.index('--cap-add=SYS_PTRACE')
        netadmin_idx = config.podman_args.index('--cap-add=NET_ADMIN')
        assert ptrace_idx < netadmin_idx


class TestDevcontainerDedup:
    """Test dc_args deduplication against podmanArgs and passthrough_args."""

    def test_dc_arg_in_passthrough_filtered(self, make_cli_args):
        """dc_args entry already in passthrough_args is removed."""
        cli = make_cli_args(passthrough_args=['--init'])
        dc = {'init': True, 'image': 'alpine'}
        config = merge_config(cli, {}, dc)
        assert config.podman_args.count('--init') == 0
        assert '--init' in config.passthrough_args

    def test_dc_arg_in_podman_args_filtered(self, make_cli_args):
        """dc_args entry already in podmanArgs is removed."""
        cli = make_cli_args()
        podrun_cfg = {'podmanArgs': ['--init']}
        dc = {'init': True, 'image': 'alpine'}
        config = merge_config(cli, podrun_cfg, dc)
        assert config.podman_args.count('--init') == 1

    def test_dc_arg_not_in_either_kept(self, make_cli_args):
        """dc_args entry NOT in podmanArgs or passthrough is kept."""
        cli = make_cli_args()
        dc = {'init': True, 'image': 'alpine'}
        config = merge_config(cli, {}, dc)
        assert '--init' in config.podman_args

    def test_partial_overlap(self, make_cli_args):
        """Multiple dc_args, partial overlap — only duplicates removed."""
        cli = make_cli_args(passthrough_args=['--init'])
        dc = {
            'init': True,
            'capAdd': ['SYS_PTRACE'],
            'image': 'alpine',
        }
        config = merge_config(cli, {}, dc)
        # --init from dc should be filtered (in passthrough)
        assert '--init' not in config.podman_args
        # --cap-add=SYS_PTRACE from dc should be kept
        assert '--cap-add=SYS_PTRACE' in config.podman_args

    def test_mount_dedup(self, make_cli_args):
        """--mount dedup: exact string match filters duplicate bind mount."""
        mount = 'type=bind,source=/host/path,target=/container/path'
        cli = make_cli_args(passthrough_args=[f'--mount={mount}'])
        dc = {'mounts': [mount], 'image': 'alpine'}
        config = merge_config(cli, {}, dc)
        mount_args = [a for a in config.podman_args if a.startswith('--mount=')]
        assert len(mount_args) == 0
        assert f'--mount={mount}' in config.passthrough_args


class TestPodmanPathConfig:
    """Test podman_path resolution via _resolve_podman_path and merge_config."""

    def test_podman_path_from_podrun_cfg(self, make_cli_args, monkeypatch):
        """podmanPath in podrun_cfg overrides default in merge_config."""
        cli = make_cli_args()
        config = merge_config(cli, {}, {'image': 'alpine'}, podman_path='/custom/bin/podman')
        assert config.podman_path == '/custom/bin/podman'

    def test_podman_path_default_when_absent(self, make_cli_args):
        """Default podman_path used when no podmanPath in config."""
        cli = make_cli_args()
        config = merge_config(cli, {}, {'image': 'alpine'}, podman_path='podman')
        assert config.podman_path == 'podman'

    def test_resolve_podman_path_from_cfg(self, monkeypatch):
        """_resolve_podman_path resolves podmanPath from podrun_cfg."""
        monkeypatch.setattr(
            podrun_mod.shutil,
            'which',
            lambda x: '/custom/bin/podman' if x == '/custom/bin/podman' else None,
        )
        result = _resolve_podman_path({'podmanPath': '/custom/bin/podman'}, 'podman')
        assert result == '/custom/bin/podman'

    def test_resolve_podman_path_absent(self):
        """_resolve_podman_path returns default when podmanPath absent."""
        result = _resolve_podman_path({}, '/usr/bin/podman')
        assert result == '/usr/bin/podman'

    def test_resolve_podman_path_not_found(self, monkeypatch):
        """_resolve_podman_path exits when podmanPath doesn't resolve."""
        monkeypatch.setattr(
            podrun_mod.shutil,
            'which',
            lambda x: 'podman' if x == 'podman' else None,
        )
        with pytest.raises(SystemExit) as exc_info:
            _resolve_podman_path({'podmanPath': '/no/such/podman'}, 'podman')
        assert exc_info.value.code == 1
