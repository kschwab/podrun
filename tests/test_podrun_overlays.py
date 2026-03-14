"""Tests for Phase 2.3 — overlay arg builders."""

import os
import pathlib
import subprocess

import pytest

import podrun.podrun as podrun_mod
from podrun.podrun import (
    BOOTSTRAP_CAPS,
    GID,
    PODRUN_ENTRYPOINT_PATH,
    PODRUN_EXEC_ENTRY_PATH,
    PODRUN_RC_PATH,
    UID,
    UNAME,
    _DOTFILES_MOUNT,
    _OVERLAY_FIELDS,
    _dot_files_overlay_args,
    _env_args,
    _host_overlay_args,
    _interactive_overlay_args,
    _podman_remote_args,
    _user_overlay_args,
    _validate_overlay_args,
    _x11_args,
    compute_caps_to_drop,
    generate_run_entrypoint,
    parse_args,
    print_overlays,
    resolve_config,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Prevent tests from picking up real devcontainer.json or store dirs."""
    monkeypatch.setattr(podrun_mod, 'find_devcontainer_json', lambda start_dir=None: None)
    monkeypatch.setattr(podrun_mod, '_default_store_dir', lambda: None)
    monkeypatch.setattr(podrun_mod, '_is_nested', lambda: False)


# ---------------------------------------------------------------------------
# compute_caps_to_drop
# ---------------------------------------------------------------------------


class TestComputeCapsToDrop:
    def test_default_returns_all_bootstrap(self):
        result = compute_caps_to_drop([])
        assert result == sorted(BOOTSTRAP_CAPS)

    def test_privileged_returns_empty(self):
        result = compute_caps_to_drop(['--privileged'])
        assert result == []

    def test_user_cap_add_equals_filtered(self):
        result = compute_caps_to_drop(['--cap-add=CAP_CHOWN'])
        assert 'CAP_CHOWN' not in result
        assert 'CAP_DAC_OVERRIDE' in result

    def test_user_cap_add_space_filtered(self):
        result = compute_caps_to_drop(['--cap-add', 'CAP_FOWNER'])
        assert 'CAP_FOWNER' not in result

    def test_user_cap_add_comma_separated(self):
        result = compute_caps_to_drop(['--cap-add=CAP_CHOWN,CAP_FOWNER'])
        assert 'CAP_CHOWN' not in result
        assert 'CAP_FOWNER' not in result
        assert 'CAP_DAC_OVERRIDE' in result

    def test_user_cap_add_case_insensitive_input(self):
        result = compute_caps_to_drop(['--cap-add=cap_chown'])
        assert 'CAP_CHOWN' not in result

    def test_non_bootstrap_cap_no_effect(self):
        result = compute_caps_to_drop(['--cap-add=CAP_SYS_ADMIN'])
        assert result == sorted(BOOTSTRAP_CAPS)

    def test_all_bootstrap_caps_filtered(self):
        pt = [f'--cap-add={c}' for c in BOOTSTRAP_CAPS]
        result = compute_caps_to_drop(pt)
        assert result == []

    def test_result_sorted(self):
        result = compute_caps_to_drop([])
        assert result == sorted(result)


# ---------------------------------------------------------------------------
# _user_overlay_args
# ---------------------------------------------------------------------------


class TestUserOverlayArgs:
    @pytest.fixture(autouse=True)
    def _tmp_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path))

    def _call(self, ns=None, pt=None):
        ns = ns or {}
        pt = pt or []
        args, caps = _user_overlay_args(ns, pt, '/tmp/ep.sh', '/tmp/rc.sh', '/tmp/exec.sh')
        return args, caps

    def test_userns_keep_id(self):
        args, _ = self._call()
        assert '--userns=keep-id' in args

    def test_userns_not_added_if_present(self):
        args, _ = self._call(pt=['--userns=auto'])
        assert '--userns=keep-id' not in args

    def test_passwd_entry(self):
        args, _ = self._call()
        passwd = [a for a in args if '--passwd-entry=' in a]
        assert len(passwd) == 1
        assert UNAME in passwd[0]
        assert str(UID) in passwd[0]
        assert str(GID) in passwd[0]

    def test_passwd_entry_not_added_if_present(self):
        args, _ = self._call(pt=['--passwd-entry=custom'])
        passwd = [a for a in args if '--passwd-entry=' in a]
        assert len(passwd) == 0

    def test_cap_add_bootstrap(self):
        args, _ = self._call()
        cap_adds = [a for a in args if a.startswith('--cap-add=')]
        cap_names = {a.split('=', 1)[1] for a in cap_adds}
        assert cap_names == set(BOOTSTRAP_CAPS)

    def test_entrypoint_set(self):
        args, _ = self._call()
        assert f'--entrypoint={PODRUN_ENTRYPOINT_PATH}' in args

    def test_script_volume_mounts(self):
        args, _ = self._call()
        assert f'-v=/tmp/ep.sh:{PODRUN_ENTRYPOINT_PATH}:ro' in args
        assert f'-v=/tmp/rc.sh:{PODRUN_RC_PATH}:ro' in args
        assert f'-v=/tmp/exec.sh:{PODRUN_EXEC_ENTRY_PATH}:ro' in args

    def test_env_rc_path(self):
        args, _ = self._call()
        assert f'--env=ENV={PODRUN_RC_PATH}' in args

    def test_returns_caps_to_drop(self):
        _, caps = self._call()
        assert caps == sorted(BOOTSTRAP_CAPS)

    def test_returns_filtered_caps_with_privileged(self):
        _, caps = self._call(pt=['--privileged'])
        assert caps == []

    def test_export_volume_mounts(self, tmp_path):
        host_dir = str(tmp_path / 'host_export')
        ns = {'run.export': [f'/data:{host_dir}']}
        args, _ = self._call(ns=ns)
        export_vols = [a for a in args if '/.podrun/exports/' in a]
        assert len(export_vols) == 1
        assert os.path.isdir(host_dir)  # makedirs called


# ---------------------------------------------------------------------------
# _interactive_overlay_args
# ---------------------------------------------------------------------------


class TestInteractiveOverlayArgs:
    def test_adds_it(self):
        args = _interactive_overlay_args({}, [])
        assert '-it' in args

    def test_skips_it_if_i_present(self):
        args = _interactive_overlay_args({}, ['-i'])
        assert '-it' not in args

    def test_skips_it_if_t_present(self):
        args = _interactive_overlay_args({}, ['-t'])
        assert '-it' not in args

    def test_skips_it_if_combined(self):
        args = _interactive_overlay_args({}, ['-it'])
        assert args.count('-it') == 0  # already present, not added again

    def test_detach_keys(self):
        args = _interactive_overlay_args({}, [])
        assert '--detach-keys=ctrl-q,ctrl-q' in args


# ---------------------------------------------------------------------------
# _host_overlay_args
# ---------------------------------------------------------------------------


class TestHostOverlayArgs:
    def test_hostname(self):
        import platform

        args = _host_overlay_args({}, [])
        hostname_args = [a for a in args if a.startswith('--hostname=')]
        assert len(hostname_args) == 1
        assert hostname_args[0] == f'--hostname={platform.node()}'

    def test_hostname_not_added_if_present(self):
        args = _host_overlay_args({}, ['--hostname=custom'])
        hostname_args = [a for a in args if a.startswith('--hostname=')]
        assert len(hostname_args) == 0

    def test_network_host(self):
        args = _host_overlay_args({}, [])
        assert '--network=host' in args

    def test_network_not_added_if_present(self):
        args = _host_overlay_args({}, ['--network=bridge'])
        assert '--network=host' not in args

    def test_seccomp_unconfined(self):
        args = _host_overlay_args({}, [])
        assert '--security-opt=seccomp=unconfined' in args

    def test_init(self):
        args = _host_overlay_args({}, [])
        assert '--init' in args

    def test_workspace_volume(self):
        ns = {'run.workspace_folder': '/app', 'run.workspace_mount_src': '/host/project'}
        args = _host_overlay_args(ns, [])
        assert '-v=/host/project:/app' in args

    def test_workdir(self):
        ns = {'run.workspace_folder': '/work'}
        args = _host_overlay_args(ns, [])
        assert '-w=/work' in args

    def test_workdir_not_added_if_present(self):
        args = _host_overlay_args({}, ['-w=/custom'])
        workdir_args = [a for a in args if a.startswith('-w=')]
        assert len(workdir_args) == 0

    def test_term_env(self):
        args = _host_overlay_args({}, [])
        assert '--env=TERM=xterm-256color' in args

    def test_default_workspace_folder(self):
        args = _host_overlay_args({}, [])
        assert any('/app' in a for a in args)

    def test_localtime_mount(self):
        args = _host_overlay_args({}, [])
        if os.path.exists('/etc/localtime'):
            assert '-v=/etc/localtime:/etc/localtime:ro' in args


# ---------------------------------------------------------------------------
# _dot_files_overlay_args
# ---------------------------------------------------------------------------


class TestDotFilesOverlayArgs:
    def test_mounts_existing_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        # Create a dotfile
        (tmp_path / '.vimrc').write_text('set nocp')
        args = _dot_files_overlay_args({}, [])
        vimrc_args = [a for a in args if '.vimrc' in a]
        assert len(vimrc_args) == 1
        assert ':ro' in vimrc_args[0]

    def test_skips_missing_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        # No dotfiles exist
        args = _dot_files_overlay_args({}, [])
        assert args == []

    def test_mounts_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        (tmp_path / '.emacs.d').mkdir()
        args = _dot_files_overlay_args({}, [])
        emacs_args = [a for a in args if '.emacs.d' in a]
        assert len(emacs_args) == 1

    def test_only_known_dotfiles(self, tmp_path, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        (tmp_path / '.random_config').write_text('x')
        args = _dot_files_overlay_args({}, [])
        assert args == []

    def test_all_dotfiles_present(self, tmp_path, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        for name in _DOTFILES_MOUNT:
            (tmp_path / name).write_text('x')
        args = _dot_files_overlay_args({}, [])
        assert len(args) == len(_DOTFILES_MOUNT)


# ---------------------------------------------------------------------------
# _x11_args
# ---------------------------------------------------------------------------


class TestX11Args:
    def test_no_x11_socket(self, monkeypatch):
        monkeypatch.setattr(pathlib.Path, 'exists', lambda self: False)
        args = _x11_args({})
        assert args == []

    def test_x11_with_socket(self, monkeypatch):
        monkeypatch.setattr(pathlib.Path, 'exists', lambda self: str(self) == '/tmp/.X11-unix')
        monkeypatch.setattr(
            podrun_mod,
            'run_os_cmd',
            lambda cmd: subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout='/home/user/.Xauthority', stderr=''
            ),
        )
        args = _x11_args({})
        assert '--env=DISPLAY' in args
        assert '-v=/tmp/.X11-unix:/tmp/.X11-unix:ro' in args
        xauth_args = [a for a in args if '.Xauthority' in a]
        assert len(xauth_args) == 1


# ---------------------------------------------------------------------------
# _podman_remote_args
# ---------------------------------------------------------------------------


class TestPodmanRemoteArgs:
    def test_store_socket(self, tmp_path):
        sock = tmp_path / 'podman.sock'
        sock.touch()
        ns = {'run.store_socket': str(sock)}
        args = _podman_remote_args(ns)
        assert f'-v={sock}:{podrun_mod.PODRUN_SOCKET_PATH}' in args
        assert f'--env=CONTAINER_HOST={podrun_mod.PODRUN_CONTAINER_HOST}' in args

    def test_fallback_systemd_socket(self, tmp_path, monkeypatch):
        """Systemd socket found at /run/user/UID/podman/podman.sock."""
        sock_dir = tmp_path / 'run' / 'user' / '12345' / 'podman'
        sock_dir.mkdir(parents=True)
        sock = sock_dir / 'podman.sock'
        sock.touch()
        monkeypatch.setattr(podrun_mod, 'UID', 12345)
        real_exists = pathlib.Path.exists

        def fake_exists(self):
            if str(self) == '/run/user/12345/podman/podman.sock':
                return True
            return real_exists(self)

        monkeypatch.setattr(pathlib.Path, 'exists', fake_exists)
        args = _podman_remote_args({})
        assert any(podrun_mod.PODRUN_SOCKET_PATH in a for a in args)
        assert any(podrun_mod.PODRUN_CONTAINER_HOST in a for a in args)

    def test_no_socket_warns(self, monkeypatch, capsys):
        monkeypatch.setattr(pathlib.Path, 'exists', lambda self: False)
        args = _podman_remote_args({})
        assert args == []
        assert 'podman.socket not found' in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _env_args
# ---------------------------------------------------------------------------


class TestEnvArgs:
    def test_overlay_tokens(self):
        ns = {'run.user_overlay': True, 'run.host_overlay': True}
        args = _env_args(ns)
        overlay_arg = [a for a in args if 'PODRUN_OVERLAYS=' in a]
        assert len(overlay_arg) == 1
        assert 'user' in overlay_arg[0]
        assert 'host' in overlay_arg[0]

    def test_no_overlays(self):
        args = _env_args({})
        overlay_arg = [a for a in args if 'PODRUN_OVERLAYS=' in a]
        assert overlay_arg == ['--env=PODRUN_OVERLAYS=none']

    def test_dotfiles_overlay_token(self):
        ns = {'run.dot_files_overlay': True, 'run.user_overlay': True}
        args = _env_args(ns)
        overlay_arg = [a for a in args if 'PODRUN_OVERLAYS=' in a][0]
        assert 'dotfiles' in overlay_arg

    def test_workdir_env(self):
        ns = {'run.host_overlay': True, 'run.workspace_folder': '/work'}
        args = _env_args(ns)
        assert '--env=PODRUN_WORKDIR=/work' in args

    def test_shell_env(self):
        ns = {'run.shell': 'zsh'}
        args = _env_args(ns)
        assert '--env=PODRUN_SHELL=zsh' in args

    def test_login_env_true(self):
        ns = {'run.login': True}
        args = _env_args(ns)
        assert '--env=PODRUN_LOGIN=1' in args

    def test_login_env_false(self):
        ns = {'run.login': False}
        args = _env_args(ns)
        assert '--env=PODRUN_LOGIN=0' in args

    def test_login_env_none(self):
        ns = {'run.login': None}
        args = _env_args(ns)
        login_args = [a for a in args if 'PODRUN_LOGIN' in a]
        assert login_args == []

    def test_image_env(self):
        ns = {'run.image': 'registry.io/org/app:v1'}
        args = _env_args(ns)
        assert '--env=PODRUN_IMG=registry.io/org/app:v1' in args
        assert '--env=PODRUN_IMG_NAME=org/app' in args
        assert '--env=PODRUN_IMG_REPO=registry.io' in args
        assert '--env=PODRUN_IMG_TAG=v1' in args

    def test_no_image(self):
        args = _env_args({})
        img_args = [a for a in args if 'PODRUN_IMG' in a]
        assert img_args == []

    def test_remote_env(self):
        ns = {'run.remote_env': {'FOO': 'bar', 'BAZ': 'qux'}}
        args = _env_args(ns)
        assert '--env=FOO=bar' in args
        assert '--env=BAZ=qux' in args


# ---------------------------------------------------------------------------
# _validate_overlay_args
# ---------------------------------------------------------------------------


class TestValidateOverlayArgs:
    def test_no_user_overlay_noop(self):
        _validate_overlay_args({})  # should not raise

    def test_user_flag_conflicts(self):
        ns = {'run.user_overlay': True, 'run.passthrough_args': ['--user=root']}
        with pytest.raises(SystemExit):
            _validate_overlay_args(ns)

    def test_short_u_flag_conflicts(self):
        ns = {'run.user_overlay': True, 'run.passthrough_args': ['-u', 'root']}
        with pytest.raises(SystemExit):
            _validate_overlay_args(ns)

    def test_combined_short_u_conflicts(self):
        ns = {'run.user_overlay': True, 'run.passthrough_args': ['-u1000']}
        with pytest.raises(SystemExit):
            _validate_overlay_args(ns)

    def test_userns_keep_id_ok(self):
        ns = {'run.user_overlay': True, 'run.passthrough_args': ['--userns=keep-id']}
        _validate_overlay_args(ns)  # should not raise

    def test_userns_other_warns(self, capsys):
        ns = {'run.user_overlay': True, 'run.passthrough_args': ['--userns=auto']}
        _validate_overlay_args(ns)
        assert 'Warning' in capsys.readouterr().err


# ---------------------------------------------------------------------------
# print_overlays
# ---------------------------------------------------------------------------


class TestPrintOverlays:
    def test_output(self, capsys):
        print_overlays()
        out = capsys.readouterr().out
        assert 'user:' in out
        assert 'host' in out
        assert 'interactive' in out
        assert 'dotfiles' in out
        assert 'workspace' in out
        assert 'adhoc' in out

    def test_dotfiles_listed(self, capsys):
        print_overlays()
        out = capsys.readouterr().out
        for name in _DOTFILES_MOUNT:
            assert name in out


# ---------------------------------------------------------------------------
# CLI integration — --dot-files-overlay
# ---------------------------------------------------------------------------


class TestDotFilesOverlayCLI:
    def test_flag_parsed(self):
        r = parse_args(['run', '--dot-files-overlay', 'alpine'])
        assert r.ns.get('run.dot_files_overlay') is True

    def test_alias_dotfiles(self):
        r = parse_args(['run', '--dotfiles', 'alpine'])
        assert r.ns.get('run.dot_files_overlay') is True

    def test_implies_user_overlay(self):
        r = parse_args(['run', '--dot-files-overlay', 'alpine'])
        r = resolve_config(r)
        assert r.ns.get('run.user_overlay') is True

    def test_in_overlay_fields(self):
        keys = [k for k, _ in _OVERLAY_FIELDS]
        assert 'run.dot_files_overlay' in keys


# ---------------------------------------------------------------------------
# generate_run_entrypoint caps_to_drop parameter
# ---------------------------------------------------------------------------


class TestEntrypointCapsToDrop:
    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path))

    def test_default_caps(self):
        path = generate_run_entrypoint({})
        with open(path) as f:
            content = f.read()
        for cap in BOOTSTRAP_CAPS:
            assert cap[4:].lower() in content

    def test_custom_caps(self):
        path = generate_run_entrypoint({}, caps_to_drop=['CAP_CHOWN'])
        with open(path) as f:
            content = f.read()
        assert 'chown' in content
        assert 'dac_override' not in content

    def test_empty_caps_no_setpriv(self):
        path = generate_run_entrypoint({}, caps_to_drop=[])
        with open(path) as f:
            content = f.read()
        # With no caps to drop, setpriv section should have empty drop string
        assert '_drop=","' not in content
