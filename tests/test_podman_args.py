import os
import re
from unittest.mock import patch

import pytest

from podrun.podrun import (
    PODRUN_EXEC_ENTRY_PATH,
    _parse_export,
    _parse_image_ref,
    _validate_overlay_args,
    build_podman_args,
    build_podman_exec_args,
    generate_run_entrypoint,
    print_overlays,
    query_container_info,
)


class TestBuildPodmanArgs:
    def test_no_image_raises(self, make_config):
        config = make_config(image=None)
        with pytest.raises(ValueError, match='config.image'):
            build_podman_args(config)

    def test_minimal(self, make_config):
        config = make_config()
        args = build_podman_args(config)
        assert args[0] == 'run'
        assert 'test-image:latest' in args

    def test_with_name(self, make_config):
        config = make_config(name='mycontainer')
        args = build_podman_args(config)
        assert '--name=mycontainer' in args

    def test_user_overlay_flags(self, make_config, podrun_tmp):
        config = make_config(
            user_overlay=True,
            bootstrap_caps=['CAP_DAC_OVERRIDE', 'CAP_CHOWN', 'CAP_FOWNER', 'CAP_SETPCAP'],
        )
        ep = str(podrun_tmp / 'ep.sh')
        rc = str(podrun_tmp / 'rc.sh')
        ee = str(podrun_tmp / 'exec-entrypoint.sh')
        args = build_podman_args(config, entrypoint_path=ep, rc_path=rc, exec_entry_path=ee)
        assert '--userns=keep-id' in args
        assert '--passwd-entry=testuser:*:1234:5678:testuser:/home/testuser:/bin/sh' in args
        assert '--entrypoint=/.podrun/run-entrypoint.sh' in args
        assert f'-v={ee}:{PODRUN_EXEC_ENTRY_PATH}:ro' in args
        # Check cap-add for bootstrap caps
        cap_adds = [a for a in args if a.startswith('--cap-add=')]
        assert len(cap_adds) == 4

    def test_interactive_flags(self, make_config):
        config = make_config(interactive_overlay=True)
        args = build_podman_args(config)
        assert '-it' in args
        assert '--detach-keys=ctrl-q,ctrl-q' in args

    def test_host_overlay_flags(self, make_config):
        config = make_config(host_overlay=True, workspace_folder='/app', workspace_mount_src='/src')
        args = build_podman_args(config)
        assert '--network=host' in args
        assert '--security-opt=seccomp=unconfined' in args
        assert '--init' in args
        assert '-v=/src:/app' in args
        assert '-w=/app' in args
        assert '--env=TERM=xterm-256color' in args

    def test_env(self, make_config):
        config = make_config(container_env={'FOO': 'bar'}, remote_env={'BAZ': 'qux'})
        args = build_podman_args(config)
        assert '--env=FOO=bar' in args
        assert '--env=BAZ=qux' in args

    def test_adhoc_adds_rm(self, make_config, podrun_tmp):
        config = make_config(
            adhoc=True,
            user_overlay=True,
            image='alpine:latest',
            bootstrap_caps=['CAP_DAC_OVERRIDE', 'CAP_CHOWN', 'CAP_FOWNER', 'CAP_SETPCAP'],
        )
        args = build_podman_args(
            config, str(podrun_tmp / 'ep'), str(podrun_tmp / 'rc'), str(podrun_tmp / 'ee')
        )
        assert '--rm' in args

    def test_command(self, make_config):
        config = make_config(command=['bash', '-c', 'echo hi'])
        args = build_podman_args(config)
        assert args[-3:] == ['bash', '-c', 'echo hi']

    def test_tilde_expansion_with_user_overlay(self, make_config, podrun_tmp):
        config = make_config(
            user_overlay=True,
            podman_args=['-v=~/src:/dest'],
        )
        ep = str(podrun_tmp / 'ep.sh')
        rc = str(podrun_tmp / 'rc.sh')
        ee = str(podrun_tmp / 'ee.sh')
        args = build_podman_args(config, entrypoint_path=ep, rc_path=rc, exec_entry_path=ee)
        expanded = [a for a in args if a.startswith('-v=/home/testuser')]
        assert len(expanded) >= 1

    def test_no_tilde_expansion_without_user_overlay(self, make_config):
        config = make_config(
            user_overlay=False,
            podman_args=['-v=~/src:/dest'],
        )
        args = build_podman_args(config)
        assert '-v=~/src:/dest' in args

    def test_x11_with_socket_and_xauth(self, make_config, mock_run_os_cmd):
        """Cover x11 branch when socket exists and xauth succeeds (lines 1017-1026)."""
        import pathlib as _pathlib

        mock_run_os_cmd.set_return(stdout='/home/user/.Xauthority\n')
        config = make_config(x11=True)
        orig_exists = _pathlib.Path.exists
        with patch.object(
            _pathlib.Path,
            'exists',
            lambda self: True if str(self) == '/tmp/.X11-unix' else orig_exists(self),
        ):
            args = build_podman_args(config)
        assert '--env=DISPLAY' in args
        assert '-v=/tmp/.X11-unix:/tmp/.X11-unix:ro' in args

    def test_x11_xauth_fails(self, make_config, mock_run_os_cmd):
        """Cover x11 branch when xauth command fails (line 1022 false)."""
        import pathlib as _pathlib

        mock_run_os_cmd.set_return(returncode=1)
        config = make_config(x11=True)
        orig_exists = _pathlib.Path.exists
        with patch.object(
            _pathlib.Path,
            'exists',
            lambda self: True if str(self) == '/tmp/.X11-unix' else orig_exists(self),
        ):
            args = build_podman_args(config)
        assert '--env=DISPLAY' not in args

    def test_x11_no_socket(self, make_config):
        """Cover x11 branch when socket doesn't exist (line 1018 false)."""
        import pathlib as _pathlib

        config = make_config(x11=True)
        orig_exists = _pathlib.Path.exists
        with patch.object(
            _pathlib.Path,
            'exists',
            lambda self: False if str(self) == '/tmp/.X11-unix' else orig_exists(self),
        ):
            args = build_podman_args(config)
        assert '--env=DISPLAY' not in args

    def test_dood_with_socket(self, make_config):
        """Cover dood branch when socket exists (lines 1030-1032)."""
        import pathlib as _pathlib

        config = make_config(dood=True)
        orig_exists = _pathlib.Path.exists
        with patch.object(
            _pathlib.Path,
            'exists',
            lambda self: True if 'podman.sock' in str(self) else orig_exists(self),
        ):
            args = build_podman_args(config)
        vol_args = [a for a in args if 'podman.sock' in a]
        assert len(vol_args) == 1

    def test_dood_no_socket(self, make_config):
        """Cover dood branch when socket doesn't exist (line 1031 false)."""
        import pathlib as _pathlib

        config = make_config(dood=True)
        orig_exists = _pathlib.Path.exists
        with patch.object(
            _pathlib.Path,
            'exists',
            lambda self: False if 'podman.sock' in str(self) else orig_exists(self),
        ):
            args = build_podman_args(config)
        vol_args = [a for a in args if 'podman.sock' in a]
        assert len(vol_args) == 0


class TestBuildPodmanExecArgs:
    def test_no_name_raises(self, make_config):
        config = make_config(name=None)
        with pytest.raises(ValueError, match='config.name'):
            build_podman_exec_args(config)

    def test_basic_invokes_exec_entry(self, make_config):
        config = make_config(name='mycontainer')
        args = build_podman_exec_args(config)
        assert args[0] == 'exec'
        assert 'mycontainer' in args
        assert args[-1] == PODRUN_EXEC_ENTRY_PATH

    def test_always_interactive(self, make_config):
        """Exec always uses -it and --detach-keys for attach."""
        config = make_config(name='mycontainer')
        args = build_podman_exec_args(config)
        assert '-it' in args
        assert '--detach-keys=ctrl-q,ctrl-q' in args

    def test_with_command(self, make_config):
        """Commands bypass exec-entrypoint.sh and run directly."""
        config = make_config(name='mycontainer', command=['ls', '-la'])
        args = build_podman_exec_args(config)
        assert args[0] == 'exec'
        assert '-it' in args
        assert '--detach-keys=ctrl-q,ctrl-q' in args
        assert 'mycontainer' in args
        name_idx = args.index('mycontainer')
        assert args[name_idx + 1 :] == ['ls', '-la']
        assert PODRUN_EXEC_ENTRY_PATH not in args

    def test_container_workdir(self, make_config):
        """Working directory comes from the container's actual config."""
        config = make_config(name='mycontainer')
        args = build_podman_exec_args(config, container_workdir='/app')
        assert '-w=/app' in args

    def test_no_workdir_when_unset(self, make_config):
        """No -w when container has no working directory."""
        config = make_config(name='mycontainer')
        args = build_podman_exec_args(config, container_workdir='')
        assert all(not a.startswith('-w=') for a in args)

    def test_stty_env_var_injected(self, make_config):
        """Exec injects PODRUN_STTY_INIT with terminal dimensions."""
        config = make_config(name='mycontainer')
        args = build_podman_exec_args(config)
        stty_args = [a for a in args if a.startswith('-e=PODRUN_STTY_INIT=')]
        assert len(stty_args) == 1
        assert 'rows' in stty_args[0]
        assert 'cols' in stty_args[0]

    def test_flags_before_name(self, make_config):
        """All flags come before the container name."""
        config = make_config(name='mycontainer')
        args = build_podman_exec_args(config, container_workdir='/app')
        name_idx = args.index('mycontainer')
        it_idx = args.index('-it')
        w_idx = args.index('-w=/app')
        assert it_idx < name_idx
        assert w_idx < name_idx

    def test_cli_shell_override_env(self, make_config):
        """When config.shell is set, -e=PODRUN_SHELL= appears."""
        config = make_config(name='mycontainer', shell='/bin/zsh')
        args = build_podman_exec_args(config)
        assert '-e=PODRUN_SHELL=/bin/zsh' in args

    def test_cli_login_override_env_true(self, make_config):
        """When config.login is True, -e=PODRUN_LOGIN=1 appears."""
        config = make_config(name='mycontainer', login=True)
        args = build_podman_exec_args(config)
        assert '-e=PODRUN_LOGIN=1' in args

    def test_cli_login_override_env_false(self, make_config):
        """When config.login is False, -e=PODRUN_LOGIN=0 appears."""
        config = make_config(name='mycontainer', login=False)
        args = build_podman_exec_args(config)
        assert '-e=PODRUN_LOGIN=0' in args

    def test_no_override_envs_by_default(self, make_config):
        """No PODRUN_SHELL or PODRUN_LOGIN in args when unset."""
        config = make_config(name='mycontainer', shell=None, login=None)
        args = build_podman_exec_args(config)
        assert not any(a.startswith('-e=PODRUN_SHELL=') for a in args)
        assert not any(a.startswith('-e=PODRUN_LOGIN=') for a in args)

    def test_env_rc_sh_passed(self, make_config):
        """ENV=/.podrun/rc.sh is passed so the shell sources it on startup."""
        config = make_config(name='mycontainer')
        args = build_podman_exec_args(config)
        assert '-e=ENV=/.podrun/rc.sh' in args

    def test_no_stty_on_terminal_error(self, make_config):
        """PODRUN_STTY_INIT is omitted when get_terminal_size raises."""
        import podrun.podrun as _mod

        with patch.object(_mod.shutil, 'get_terminal_size', side_effect=OSError):
            config = make_config(name='mycontainer')
            args = build_podman_exec_args(config)
        assert not any(a.startswith('-e=PODRUN_STTY_INIT=') for a in args)


class TestQueryContainerInfo:
    def test_fallback_on_failure(self, mock_run_os_cmd):
        mock_run_os_cmd.set_return(returncode=1)
        workdir, overlays = query_container_info('test')
        assert workdir == ''
        assert overlays == ''

    def test_global_flags_in_inspect(self, mock_run_os_cmd):
        """Global flags are included in the podman inspect command."""
        mock_run_os_cmd.set_return(
            stdout='PODRUN_WORKDIR=/app\nPODRUN_OVERLAYS=user,host\n',
        )
        workdir, overlays = query_container_info(
            'test', global_flags=['--root=/store', '--runroot=/run']
        )
        cmd = mock_run_os_cmd.calls[0]
        assert '--root=/store' in cmd
        assert '--runroot=/run' in cmd
        assert 'inspect' in cmd
        assert workdir == '/app'
        assert overlays == 'user,host'


class TestFlagDedup:
    """Test flag deduplication in build_podman_args."""

    @pytest.mark.parametrize(
        'overlay,passthrough,prefixes,kept',
        [
            # host_overlay flags
            (
                {'host_overlay': True},
                ['--hostname=custom-host'],
                ('--hostname=',),
                '--hostname=custom-host',
            ),
            ({'host_overlay': True}, ['--network=bridge'], ('--network=',), '--network=bridge'),
            ({'host_overlay': True}, ['-w=/custom'], ('-w=', '--workdir='), '-w=/custom'),
            (
                {'host_overlay': True},
                ['--workdir=/custom'],
                ('-w=', '--workdir='),
                '--workdir=/custom',
            ),
            ({'host_overlay': True}, ['--init'], ('--init',), '--init'),
            # interactive_overlay flags
            ({'interactive_overlay': True}, ['-it'], ('-it',), '-it'),
        ],
        ids=[
            'hostname',
            'network',
            'workdir-short',
            'workdir-long',
            'init',
            'interactive-combined',
        ],
    )
    def test_overlay_flag_deduped(self, overlay, passthrough, prefixes, kept, make_config):
        """User passthrough flag takes precedence; overlay doesn't duplicate."""
        config = make_config(**overlay, passthrough_args=passthrough)
        args = build_podman_args(config)
        matches = [a for a in args if any(a.startswith(p) or a == p for p in prefixes)]
        assert len(matches) == 1, f'expected 1 match for {prefixes}, got {matches}'
        assert kept in args

    def test_userns_dedup(self, make_config, podrun_tmp):
        """When passthrough has --userns=auto, overlay doesn't add --userns=keep-id."""
        config = make_config(
            user_overlay=True,
            passthrough_args=['--userns=auto'],
        )
        ep = str(podrun_tmp / 'ep.sh')
        rc = str(podrun_tmp / 'rc.sh')
        ee = str(podrun_tmp / 'ee.sh')
        args = build_podman_args(config, entrypoint_path=ep, rc_path=rc, exec_entry_path=ee)
        assert '--userns=keep-id' not in args
        assert '--userns=auto' in args

    def test_userns_no_dedup_when_absent(self, make_config, podrun_tmp):
        """When passthrough has no --userns, overlay adds --userns=keep-id."""
        config = make_config(user_overlay=True)
        ep = str(podrun_tmp / 'ep.sh')
        rc = str(podrun_tmp / 'rc.sh')
        ee = str(podrun_tmp / 'ee.sh')
        args = build_podman_args(config, entrypoint_path=ep, rc_path=rc, exec_entry_path=ee)
        assert '--userns=keep-id' in args

    @pytest.mark.parametrize(
        'passthrough,absent',
        [
            (['-i'], '-it'),
            (['-t'], '-it'),
        ],
        ids=['separate-i', 'separate-t'],
    )
    def test_interactive_dedup_separate_flags(self, passthrough, absent, make_config):
        """When passthrough has -i or -t alone, overlay doesn't add -it."""
        config = make_config(
            interactive_overlay=True,
            passthrough_args=passthrough,
        )
        args = build_podman_args(config)
        assert absent not in args
        assert passthrough[0] in args

    def test_security_opt_dedup_exact(self, make_config):
        """When passthrough has exact security-opt match, overlay doesn't add."""
        config = make_config(
            host_overlay=True,
            passthrough_args=['--security-opt=seccomp=unconfined'],
        )
        args = build_podman_args(config)
        secopt = [a for a in args if a == '--security-opt=seccomp=unconfined']
        assert len(secopt) == 1

    def test_security_opt_different_value_additive(self, make_config):
        """Different security-opt values are both present (additive)."""
        config = make_config(
            host_overlay=True,
            passthrough_args=['--security-opt=label=disable'],
        )
        args = build_podman_args(config)
        assert '--security-opt=seccomp=unconfined' in args
        assert '--security-opt=label=disable' in args

    def test_env_term_dedup(self, make_config):
        """When passthrough has --env=TERM=xterm-256color, overlay doesn't add."""
        config = make_config(
            host_overlay=True,
            passthrough_args=['--env=TERM=xterm-256color'],
        )
        args = build_podman_args(config)
        term_envs = [a for a in args if a == '--env=TERM=xterm-256color']
        assert len(term_envs) == 1


class TestParseImageRef:
    """Test _parse_image_ref image reference parsing."""

    @pytest.mark.parametrize(
        'image,expected',
        [
            ('alpine:latest', ('docker.io', 'alpine', 'latest')),
            ('alpine', ('docker.io', 'alpine', 'latest')),
            ('ubuntu:24.04', ('docker.io', 'ubuntu', '24.04')),
            (
                'ssd-docker-dev-local.boartifactory.micron.com/plato/vos7ish:6',
                ('ssd-docker-dev-local.boartifactory.micron.com', 'plato/vos7ish', '6'),
            ),
            ('localhost:5000/myapp:v1', ('localhost:5000', 'myapp', 'v1')),
            ('localhost/myapp:v1', ('localhost', 'myapp', 'v1')),
            ('library/alpine:3.19', ('docker.io', 'library/alpine', '3.19')),
            ('registry.io/org/app', ('registry.io', 'org/app', 'latest')),
        ],
    )
    def test_parse(self, image, expected):
        assert _parse_image_ref(image) == expected

    def test_invalid_image_raises(self):
        with pytest.raises(ValueError, match='Invalid image name'):
            _parse_image_ref('---invalid')


class TestParseExport:
    """Test _parse_export export spec parsing."""

    @pytest.mark.parametrize(
        'entry,expected',
        [
            ('/opt/sdk:./sdk', ('/opt/sdk', './sdk', False)),
            ('/opt/sdk:./sdk:0', ('/opt/sdk', './sdk', True)),
            ('/etc/profile:/tmp/out', ('/etc/profile', '/tmp/out', False)),
            ('/etc/profile:/tmp/out:0', ('/etc/profile', '/tmp/out', True)),
        ],
    )
    def test_parse(self, entry, expected):
        assert _parse_export(entry) == expected

    def test_invalid_option_raises(self):
        with pytest.raises(ValueError, match='Invalid export spec'):
            _parse_export('/a:/b:ro')

    def test_too_many_parts_raises(self):
        with pytest.raises(ValueError, match='Invalid export spec'):
            _parse_export('/a:/b:0:extra')


class TestEnvRcShExport:
    """Test ENV=/.podrun/rc.sh is exported for sh/dash exec sessions."""

    def test_env_rc_sh_in_user_overlay(self, make_config, podrun_tmp):
        config = make_config(
            user_overlay=True,
            bootstrap_caps=['CAP_DAC_OVERRIDE', 'CAP_CHOWN', 'CAP_FOWNER', 'CAP_SETPCAP'],
        )
        ep = str(podrun_tmp / 'ep.sh')
        rc = str(podrun_tmp / 'rc.sh')
        ee = str(podrun_tmp / 'ee.sh')
        args = build_podman_args(config, entrypoint_path=ep, rc_path=rc, exec_entry_path=ee)
        assert '--env=ENV=/.podrun/rc.sh' in args


class TestPodrunEnvVars:
    """Test PODRUN_* environment variables injected when overlays are active."""

    def test_user_overlay_exports_env(self, make_config, podrun_tmp):
        config = make_config(
            user_overlay=True,
            image='registry.io/org/app:v2',
            bootstrap_caps=['CAP_DAC_OVERRIDE', 'CAP_CHOWN', 'CAP_FOWNER', 'CAP_SETPCAP'],
        )
        args = build_podman_args(
            config, str(podrun_tmp / 'ep'), str(podrun_tmp / 'rc'), str(podrun_tmp / 'ee')
        )
        assert '--env=PODRUN_OVERLAYS=user' in args
        assert '--env=PODRUN_IMG=registry.io/org/app:v2' in args
        assert '--env=PODRUN_IMG_NAME=org/app' in args
        assert '--env=PODRUN_IMG_REPO=registry.io' in args
        assert '--env=PODRUN_IMG_TAG=v2' in args

    def test_all_overlays_string(self, make_config, podrun_tmp):
        config = make_config(
            user_overlay=True,
            host_overlay=True,
            interactive_overlay=True,
            image='alpine:latest',
            bootstrap_caps=['CAP_DAC_OVERRIDE', 'CAP_CHOWN', 'CAP_FOWNER', 'CAP_SETPCAP'],
        )
        args = build_podman_args(
            config, str(podrun_tmp / 'ep'), str(podrun_tmp / 'rc'), str(podrun_tmp / 'ee')
        )
        assert '--env=PODRUN_OVERLAYS=user,host,interactive' in args

    def test_user_interactive_overlay_string(self, make_config, podrun_tmp):
        config = make_config(
            user_overlay=True,
            interactive_overlay=True,
            image='alpine:latest',
            bootstrap_caps=['CAP_DAC_OVERRIDE', 'CAP_CHOWN', 'CAP_FOWNER', 'CAP_SETPCAP'],
        )
        args = build_podman_args(
            config, str(podrun_tmp / 'ep'), str(podrun_tmp / 'rc'), str(podrun_tmp / 'ee')
        )
        assert '--env=PODRUN_OVERLAYS=user,interactive' in args

    def test_workspace_overlay_string(self, make_config, podrun_tmp):
        config = make_config(
            user_overlay=True,
            host_overlay=True,
            interactive_overlay=True,
            workspace=True,
            image='alpine:latest',
            bootstrap_caps=['CAP_DAC_OVERRIDE', 'CAP_CHOWN', 'CAP_FOWNER', 'CAP_SETPCAP'],
        )
        args = build_podman_args(
            config, str(podrun_tmp / 'ep'), str(podrun_tmp / 'rc'), str(podrun_tmp / 'ee')
        )
        assert '--env=PODRUN_OVERLAYS=user,host,interactive,workspace' in args

    def test_adhoc_overlay_string(self, make_config, podrun_tmp):
        config = make_config(
            user_overlay=True,
            host_overlay=True,
            interactive_overlay=True,
            workspace=True,
            adhoc=True,
            image='alpine:latest',
            bootstrap_caps=['CAP_DAC_OVERRIDE', 'CAP_CHOWN', 'CAP_FOWNER', 'CAP_SETPCAP'],
        )
        args = build_podman_args(
            config, str(podrun_tmp / 'ep'), str(podrun_tmp / 'rc'), str(podrun_tmp / 'ee')
        )
        assert '--env=PODRUN_OVERLAYS=user,host,interactive,workspace,adhoc' in args

    def test_no_overlay_exports_none(self, make_config):
        config = make_config(image='alpine:latest')
        args = build_podman_args(config)
        assert '--env=PODRUN_OVERLAYS=none' in args
        assert '--env=PODRUN_IMG=alpine:latest' in args

    def test_simple_image_defaults(self, make_config, podrun_tmp):
        config = make_config(
            user_overlay=True,
            image='alpine',
            bootstrap_caps=['CAP_DAC_OVERRIDE', 'CAP_CHOWN', 'CAP_FOWNER', 'CAP_SETPCAP'],
        )
        args = build_podman_args(
            config, str(podrun_tmp / 'ep'), str(podrun_tmp / 'rc'), str(podrun_tmp / 'ee')
        )
        assert '--env=PODRUN_IMG=alpine' in args
        assert '--env=PODRUN_IMG_NAME=alpine' in args
        assert '--env=PODRUN_IMG_REPO=docker.io' in args
        assert '--env=PODRUN_IMG_TAG=latest' in args

    def test_shell_env_var(self, make_config):
        config = make_config(image='alpine:latest', shell='zsh')
        args = build_podman_args(config)
        assert '--env=PODRUN_SHELL=zsh' in args

    def test_login_env_var_true(self, make_config):
        config = make_config(image='alpine:latest', login=True)
        args = build_podman_args(config)
        assert '--env=PODRUN_LOGIN=1' in args

    def test_login_env_var_false(self, make_config):
        config = make_config(image='alpine:latest', login=False)
        args = build_podman_args(config)
        assert '--env=PODRUN_LOGIN=0' in args

    def test_login_env_var_none(self, make_config):
        config = make_config(image='alpine:latest', login=None)
        args = build_podman_args(config)
        assert not any(a.startswith('--env=PODRUN_LOGIN=') for a in args)


class TestExportVolumeMounts:
    """Test --export adds volume mounts to podman args."""

    def test_export_adds_volume_mount(self, make_config, podrun_tmp, tmp_path):
        import hashlib as _hl

        host_dir = str(tmp_path / 'sdk')
        config = make_config(
            user_overlay=True,
            exports=[f'/opt/sdk/bin:{host_dir}'],
            bootstrap_caps=['CAP_DAC_OVERRIDE', 'CAP_CHOWN', 'CAP_FOWNER', 'CAP_SETPCAP'],
        )
        ep = str(podrun_tmp / 'ep.sh')
        rc = str(podrun_tmp / 'rc.sh')
        ee = str(podrun_tmp / 'ee.sh')
        args = build_podman_args(config, entrypoint_path=ep, rc_path=rc, exec_entry_path=ee)
        staging_hash = _hl.sha256('/opt/sdk/bin'.encode()).hexdigest()[:12]
        expected = f'-v={host_dir}:/.podrun/exports/{staging_hash}'
        assert expected in args

    def test_export_multiple(self, make_config, podrun_tmp, tmp_path):
        import hashlib as _hl

        dir_a = str(tmp_path / 'a')
        dir_b = str(tmp_path / 'b')
        config = make_config(
            user_overlay=True,
            exports=[f'/opt/a:{dir_a}', f'/opt/b:{dir_b}'],
            bootstrap_caps=['CAP_DAC_OVERRIDE', 'CAP_CHOWN', 'CAP_FOWNER', 'CAP_SETPCAP'],
        )
        ep = str(podrun_tmp / 'ep.sh')
        rc = str(podrun_tmp / 'rc.sh')
        ee = str(podrun_tmp / 'ee.sh')
        args = build_podman_args(config, entrypoint_path=ep, rc_path=rc, exec_entry_path=ee)
        vol_exports = [a for a in args if '/.podrun/exports/' in a]
        assert len(vol_exports) == 2
        hash_a = _hl.sha256('/opt/a'.encode()).hexdigest()[:12]
        hash_b = _hl.sha256('/opt/b'.encode()).hexdigest()[:12]
        assert f'-v={dir_a}:/.podrun/exports/{hash_a}' in args
        assert f'-v={dir_b}:/.podrun/exports/{hash_b}' in args

    def test_export_creates_host_dir(self, make_config, podrun_tmp, tmp_path):
        host_dir = str(tmp_path / 'new-export-dir')
        assert not os.path.exists(host_dir)
        config = make_config(
            user_overlay=True,
            exports=[f'/opt/sdk:{host_dir}'],
            bootstrap_caps=['CAP_DAC_OVERRIDE', 'CAP_CHOWN', 'CAP_FOWNER', 'CAP_SETPCAP'],
        )
        ep = str(podrun_tmp / 'ep.sh')
        rc = str(podrun_tmp / 'rc.sh')
        ee = str(podrun_tmp / 'ee.sh')
        build_podman_args(config, entrypoint_path=ep, rc_path=rc, exec_entry_path=ee)
        assert os.path.isdir(host_dir)

    def test_export_copy_only_same_volume_mount(self, make_config, podrun_tmp, tmp_path):
        """Copy-only (:0) still gets the same volume mount."""
        import hashlib as _hl

        host_dir = str(tmp_path / 'sdk')
        config = make_config(
            user_overlay=True,
            exports=[f'/opt/sdk/bin:{host_dir}:0'],
            bootstrap_caps=['CAP_DAC_OVERRIDE', 'CAP_CHOWN', 'CAP_FOWNER', 'CAP_SETPCAP'],
        )
        ep = str(podrun_tmp / 'ep.sh')
        rc = str(podrun_tmp / 'rc.sh')
        ee = str(podrun_tmp / 'ee.sh')
        args = build_podman_args(config, entrypoint_path=ep, rc_path=rc, exec_entry_path=ee)
        staging_hash = _hl.sha256('/opt/sdk/bin'.encode()).hexdigest()[:12]
        expected = f'-v={host_dir}:/.podrun/exports/{staging_hash}'
        assert expected in args

    def test_export_entrypoint_and_args_hashes_agree(self, make_config, podrun_tmp, tmp_path):
        """Entrypoint staging paths and volume mount targets use the same hash."""
        config = make_config(
            user_overlay=True,
            exports=[f'/opt/sdk:{tmp_path / "sdk"}', f'/etc/conf:{tmp_path / "conf"}'],
            bootstrap_caps=['CAP_DAC_OVERRIDE', 'CAP_CHOWN', 'CAP_FOWNER', 'CAP_SETPCAP'],
        )
        ep_path = generate_run_entrypoint(config)
        with open(ep_path) as f:
            ep_content = f.read()
        args = build_podman_args(
            config,
            entrypoint_path=ep_path,
            rc_path=str(podrun_tmp / 'rc.sh'),
            exec_entry_path=str(podrun_tmp / 'ee.sh'),
        )

        vol_hashes = set(
            re.search(r'/.podrun/exports/([0-9a-f]+)$', a).group(1)
            for a in args
            if '/.podrun/exports/' in a
        )
        ep_hashes = set(re.findall(r'/.podrun/exports/([0-9a-f]{12})', ep_content))

        assert vol_hashes == ep_hashes
        assert len(vol_hashes) == 2


class TestDevcontainerArgsInFinalCommand:
    """Test that devcontainer-derived args in config.podman_args flow through build_podman_args."""

    def test_mount_in_final_args(self, make_config):
        config = make_config(podman_args=['--mount=type=bind,source=/a,target=/b'])
        args = build_podman_args(config)
        assert '--mount=type=bind,source=/a,target=/b' in args

    def test_cap_add_in_final_args(self, make_config):
        config = make_config(podman_args=['--cap-add=SYS_PTRACE'])
        args = build_podman_args(config)
        assert '--cap-add=SYS_PTRACE' in args

    def test_security_opt_in_final_args(self, make_config):
        config = make_config(podman_args=['--security-opt=seccomp=unconfined'])
        args = build_podman_args(config)
        assert '--security-opt=seccomp=unconfined' in args

    def test_privileged_in_final_args(self, make_config):
        config = make_config(podman_args=['--privileged'])
        args = build_podman_args(config)
        assert '--privileged' in args

    def test_init_in_final_args(self, make_config):
        config = make_config(podman_args=['--init'])
        args = build_podman_args(config)
        assert '--init' in args


class TestPrintOverlays:
    def test_output_contains_groups(self, capsys):
        print_overlays()
        out = capsys.readouterr().out
        assert 'user:' in out
        assert 'host' in out
        assert 'interactive:' in out
        assert 'workspace' in out
        assert 'adhoc' in out


class TestOverlayConflictGuards:
    """Test _validate_overlay_args detects conflicts with user-overlay."""

    def test_user_equals_root_with_overlay_exits(self, make_config):
        """--user=root + user_overlay → sys.exit(1)."""
        config = make_config(user_overlay=True, podman_args=['--user=root'])
        with pytest.raises(SystemExit, match='1'):
            _validate_overlay_args(config)

    def test_user_space_separated_with_overlay_exits(self, make_config):
        """--user root (space-separated) + user_overlay → sys.exit(1)."""
        config = make_config(user_overlay=True, passthrough_args=['--user', 'root'])
        with pytest.raises(SystemExit, match='1'):
            _validate_overlay_args(config)

    def test_short_u_with_overlay_exits(self, make_config):
        """-u root + user_overlay → sys.exit(1)."""
        config = make_config(user_overlay=True, passthrough_args=['-u', 'root'])
        with pytest.raises(SystemExit, match='1'):
            _validate_overlay_args(config)

    def test_short_u_combined_with_overlay_exits(self, make_config):
        """-uroot (combined form) + user_overlay → sys.exit(1)."""
        config = make_config(user_overlay=True, passthrough_args=['-uroot'])
        with pytest.raises(SystemExit, match='1'):
            _validate_overlay_args(config)

    def test_userns_not_keepid_warns(self, make_config, capsys):
        """--userns=auto + user_overlay → warning to stderr (no exit)."""
        config = make_config(user_overlay=True, passthrough_args=['--userns=auto'])
        _validate_overlay_args(config)
        captured = capsys.readouterr()
        assert 'Warning' in captured.err
        assert '--userns=auto' in captured.err

    def test_user_without_overlay_no_error(self, make_config):
        """--user=root without user_overlay → no error."""
        config = make_config(user_overlay=False, podman_args=['--user=root'])
        _validate_overlay_args(config)  # should not raise

    def test_userns_keepid_no_warning(self, make_config, capsys):
        """--userns=keep-id + user_overlay → no warning."""
        config = make_config(user_overlay=True, passthrough_args=['--userns=keep-id'])
        _validate_overlay_args(config)
        captured = capsys.readouterr()
        assert 'Warning' not in captured.err
