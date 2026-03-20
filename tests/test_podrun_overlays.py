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
    _DOTFILES,
    _OVERLAY_FIELDS,
    _copy_staging_args,
    _extract_copy_staging,
    _dot_files_overlay_args,
    _env_args,
    _find_root_git_dir,
    _git_submodule_args,
    _host_overlay_args,
    _interactive_overlay_args,
    _podman_remote_args,
    _resolve_git_submodule,
    _user_overlay_args,
    devcontainer_run_args,
    _validate_overlay_args,
    _x11_args,
    compute_caps_to_drop,
    generate_run_entrypoint,
    parse_args,
    print_overlays,
    resolve_config,
)


pytestmark = pytest.mark.usefixtures('podman_binary')


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
        assert f'-v=/tmp/ep.sh:{PODRUN_ENTRYPOINT_PATH}:ro,z' in args
        assert f'-v=/tmp/rc.sh:{PODRUN_RC_PATH}:ro,z' in args
        assert f'-v=/tmp/exec.sh:{PODRUN_EXEC_ENTRY_PATH}:ro,z' in args

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

    def test_init(self):
        args = _interactive_overlay_args({}, [])
        assert '--init' in args

    def test_init_skipped_if_present(self):
        args = _interactive_overlay_args({}, ['--init'])
        assert args.count('--init') == 0


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

    def test_no_init(self):
        """--init is in interactive overlay, not host overlay."""
        args = _host_overlay_args({}, [])
        assert '--init' not in args

    def test_workspace_volume_auto(self):
        """No -w in passthrough → auto -v= and -w= from dc.workspace_folder."""
        ns = {'dc.workspace_folder': '/app'}
        args = _host_overlay_args(ns, [])
        cwd = str(pathlib.Path.cwd())
        assert f'-v={cwd}:/app:z' in args
        assert '-w=/app' in args

    def test_workspace_skipped_when_w_in_passthrough(self):
        """-w already in passthrough → no auto mount or -w."""
        args = _host_overlay_args({}, ['-w=/custom'])
        assert not any(a.startswith('-w=') for a in args)
        assert not any(a.startswith('-v=') and '/app' in a for a in args)

    def test_workspace_skipped_when_workdir_in_passthrough(self):
        """--workdir already in passthrough → no auto mount or -w."""
        args = _host_overlay_args({}, ['--workdir=/custom'])
        assert not any(a.startswith('-w=') for a in args)
        assert not any(a.startswith('-v=') and '/app' in a for a in args)

    def test_term_env(self):
        args = _host_overlay_args({}, [])
        assert '--env=TERM=xterm-256color' in args

    def test_default_workspace_folder(self):
        """No dc.workspace_folder → defaults to /app."""
        args = _host_overlay_args({}, [])
        assert '-w=/app' in args
        cwd = str(pathlib.Path.cwd())
        assert f'-v={cwd}:/app:z' in args

    def test_localtime_mount(self):
        args = _host_overlay_args({}, [])
        if os.path.exists('/etc/localtime'):
            assert '-v=/etc/localtime:/etc/localtime:ro' in args

    def test_auto_mount_skipped_if_target_already_mounted(self):
        """Target already mounted in passthrough → skip auto -v= but still add -w=."""
        ns = {'dc.workspace_folder': '/app'}
        pt = ['--mount=source=/host,target=/app,type=bind']
        args = _host_overlay_args(ns, pt)
        assert not any(a.startswith('-v=') and '/app' in a for a in args)
        assert '-w=/app' in args


# ---------------------------------------------------------------------------
# _dot_files_overlay_args
# ---------------------------------------------------------------------------


class TestDotFilesOverlayArgs:
    @pytest.fixture(autouse=True)
    def _fake_home(self, tmp_path, monkeypatch):
        """Point both USER_HOME and $HOME at tmp_path so expanduser works."""
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        monkeypatch.setenv('HOME', str(tmp_path))

    def test_mounts_existing_files(self, tmp_path):
        (tmp_path / '.vimrc').write_text('set nocp')
        args = _dot_files_overlay_args({}, [])
        vimrc_args = [a for a in args if '.vimrc' in a]
        assert len(vimrc_args) == 1
        assert ':ro,z' in vimrc_args[0]

    def test_skips_missing_files(self, tmp_path):
        args = _dot_files_overlay_args({}, [])
        assert args == []

    def test_mounts_directory(self, tmp_path):
        (tmp_path / '.emacs.d').mkdir()
        args = _dot_files_overlay_args({}, [])
        emacs_args = [a for a in args if '.emacs.d' in a]
        assert len(emacs_args) == 1

    def test_only_known_dotfiles(self, tmp_path):
        (tmp_path / '.random_config').write_text('x')
        args = _dot_files_overlay_args({}, [])
        assert args == []

    def test_all_dotfiles_present(self, tmp_path):
        for arg in _DOTFILES:
            # Extract host path from -v=host:ctr:mode
            name = arg.split('=')[1].split(':')[0].removeprefix('~/')
            p = tmp_path / name
            if not p.exists():
                p.write_text('x') if '.' in name else p.mkdir()
        args = _dot_files_overlay_args({}, [])
        assert len(args) == len(_DOTFILES)

    def test_copy_mode_emits_zero_suffix(self, tmp_path):
        """:0 items emit raw -v=...:0 args (resolved downstream)."""
        (tmp_path / '.ssh').mkdir()
        args = _dot_files_overlay_args({}, [])
        ssh_args = [a for a in args if '.ssh' in a]
        assert len(ssh_args) == 1
        assert ssh_args[0].endswith(':0')

    def test_copy_mode_gitconfig(self, tmp_path):
        (tmp_path / '.gitconfig').write_text('[user]')
        args = _dot_files_overlay_args({}, [])
        gc_args = [a for a in args if '.gitconfig' in a]
        assert len(gc_args) == 1
        assert gc_args[0].endswith(':0')

    def test_copy_mode_skips_missing(self, tmp_path):
        args = _dot_files_overlay_args({}, [])
        zero_args = [a for a in args if a.endswith(':0')]
        assert zero_args == []

    def test_mount_and_copy_together(self, tmp_path):
        (tmp_path / '.vimrc').write_text('set nocp')
        (tmp_path / '.gitconfig').write_text('[user]')
        args = _dot_files_overlay_args({}, [])
        ro_args = [a for a in args if a.endswith(':ro,z')]
        zero_args = [a for a in args if a.endswith(':0')]
        assert len(ro_args) == 1  # .vimrc
        assert len(zero_args) == 1  # .gitconfig


# ---------------------------------------------------------------------------
# _copy_staging_args
# ---------------------------------------------------------------------------


class TestCopyStagingArgs:
    def test_file_staging(self, tmp_path):
        """File item creates staging dir with data file."""
        f = tmp_path / 'config'
        f.write_text('content')
        args = _copy_staging_args([(str(f), '/home/user/.gitconfig')])
        assert len(args) == 1
        assert 'copy-staging' in args[0]
        assert ':ro,z' in args[0]
        # Verify staging content
        staging_path = args[0].split('=')[1].split(':')[0]
        assert open(os.path.join(staging_path, '.podrun_target')).read() == '/home/user/.gitconfig'
        assert open(os.path.join(staging_path, 'data')).read() == 'content'

    def test_dir_staging(self, tmp_path):
        """Directory item creates two mounts (staging + data)."""
        d = tmp_path / 'ssh'
        d.mkdir()
        (d / 'config').write_text('Host *')
        args = _copy_staging_args([(str(d), '/home/user/.ssh')])
        assert len(args) == 2
        # First mount: staging dir
        assert 'copy-staging' in args[0] and ':ro,z' in args[0]
        # Second mount: data bind
        assert '/data:ro,z' in args[1]
        # Verify target file
        staging_path = args[0].split('=')[1].split(':')[0]
        assert open(os.path.join(staging_path, '.podrun_target')).read() == '/home/user/.ssh'

    def test_empty_items(self):
        assert _copy_staging_args([]) == []

    def test_multiple_items(self, tmp_path):
        """Multiple items produce correct number of mounts."""
        f = tmp_path / 'gitconfig'
        f.write_text('[user]')
        d = tmp_path / 'ssh'
        d.mkdir()
        args = _copy_staging_args(
            [
                (str(f), '/home/user/.gitconfig'),
                (str(d), '/home/user/.ssh'),
            ]
        )
        # File: 1 mount, Dir: 2 mounts = 3 total
        assert len(args) == 3

    def test_staging_dirs_have_unique_shas(self, tmp_path):
        """Different container paths get different staging dirs."""
        f1 = tmp_path / 'a'
        f1.write_text('a')
        f2 = tmp_path / 'b'
        f2.write_text('b')
        args = _copy_staging_args(
            [
                (str(f1), '/home/user/.gitconfig'),
                (str(f2), '/home/user/.other'),
            ]
        )
        paths = [a.split('=')[1].split(':')[0] for a in args]
        assert paths[0] != paths[1]


# ---------------------------------------------------------------------------
# _extract_copy_staging
# ---------------------------------------------------------------------------


class TestExtractCopyStaging:
    def test_extracts_zero_suffix_equals_form(self):
        args = ['-v=/host/.ssh:/ctr/.ssh:0', '-v=/a:/b:ro']
        filtered, items = _extract_copy_staging(args)
        assert filtered == ['-v=/a:/b:ro']
        assert items == [('/host/.ssh', '/ctr/.ssh')]

    def test_extracts_zero_suffix_space_form(self):
        args = ['-v', '/host/.ssh:/ctr/.ssh:0', '-v=/a:/b:ro']
        filtered, items = _extract_copy_staging(args)
        assert filtered == ['-v=/a:/b:ro']
        assert items == [('/host/.ssh', '/ctr/.ssh')]

    def test_preserves_non_zero_args(self):
        args = ['-v=/a:/b:ro', '--rm', '-e', 'FOO=bar']
        filtered, items = _extract_copy_staging(args)
        assert filtered == args
        assert items == []

    def test_empty(self):
        filtered, items = _extract_copy_staging([])
        assert filtered == []
        assert items == []

    def test_multiple_zero_items(self):
        args = ['-v=/a:/b:0', '-v=/c:/d:0', '-v=/e:/f:ro']
        filtered, items = _extract_copy_staging(args)
        assert filtered == ['-v=/e:/f:ro']
        assert len(items) == 2

    def test_volume_long_form(self):
        args = ['--volume=/host:/ctr:0']
        filtered, items = _extract_copy_staging(args)
        assert filtered == []
        assert items == [('/host', '/ctr')]


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
        ns = {'run.host_overlay': True, 'dc.workspace_folder': '/work'}
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

    def test_container_env(self):
        ns = {'run.container_env': {'FOO': 'bar', 'BAZ': 'qux'}}
        args = _env_args(ns)
        assert '--env=FOO=bar' in args
        assert '--env=BAZ=qux' in args

    def test_devcontainer_cli_env(self):
        ns = {'internal.dc_from_cli': True}
        args = _env_args(ns)
        assert '--env=PODRUN_DEVCONTAINER_CLI=1' in args

    def test_devcontainer_cli_env_absent(self):
        args = _env_args({})
        dc_args = [a for a in args if 'DEVCONTAINER_CLI' in a]
        assert dc_args == []


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
        assert 'session' in out
        assert 'adhoc' in out

    def test_dotfiles_listed(self, capsys):
        print_overlays()
        out = capsys.readouterr().out
        for arg in _DOTFILES:
            assert arg in out


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


# ---------------------------------------------------------------------------
# _resolve_git_submodule
# ---------------------------------------------------------------------------


class TestResolveGitSubmodule:
    def test_directory_returns_none(self, tmp_path):
        (tmp_path / '.git').mkdir()
        assert _resolve_git_submodule(str(tmp_path)) is None

    def test_file_with_gitdir(self, tmp_path):
        git_objects = tmp_path / 'parent' / '.git' / 'modules' / 'sub'
        git_objects.mkdir(parents=True)
        (tmp_path / 'sub').mkdir()
        (tmp_path / 'sub' / '.git').write_text(f'gitdir: {git_objects}\n')
        result = _resolve_git_submodule(str(tmp_path / 'sub'))
        assert result == str(git_objects)

    def test_file_without_gitdir_prefix(self, tmp_path):
        (tmp_path / '.git').write_text('not a gitdir pointer\n')
        assert _resolve_git_submodule(str(tmp_path)) is None

    def test_no_dot_git_returns_none(self, tmp_path):
        assert _resolve_git_submodule(str(tmp_path)) is None

    def test_relative_path_resolved(self, tmp_path):
        git_objects = tmp_path / 'parent' / '.git' / 'modules' / 'sub'
        git_objects.mkdir(parents=True)
        sub_dir = tmp_path / 'parent' / 'sub'
        sub_dir.mkdir()
        (sub_dir / '.git').write_text('gitdir: ../.git/modules/sub\n')
        result = _resolve_git_submodule(str(sub_dir))
        assert result == str(git_objects)


# ---------------------------------------------------------------------------
# _find_root_git_dir
# ---------------------------------------------------------------------------


class TestFindRootGitDir:
    def test_modules_subpath(self):
        root, sub = _find_root_git_dir('/parent/.git/modules/child')
        assert root == '/parent/.git'
        assert sub == 'modules/child'

    def test_nested_modules(self):
        root, sub = _find_root_git_dir('/parent/.git/modules/a/modules/b')
        assert root == '/parent/.git'
        assert sub == 'modules/a/modules/b'

    def test_bare_git_dir(self):
        root, sub = _find_root_git_dir('/parent/.git')
        assert root == '/parent/.git'
        assert sub == ''

    def test_no_git_component(self):
        root, sub = _find_root_git_dir('/some/random/path')
        assert root is None
        assert sub is None

    def test_deep_nesting(self):
        root, sub = _find_root_git_dir('/repo/.git/modules/a/modules/b/modules/c')
        assert root == '/repo/.git'
        assert sub == 'modules/a/modules/b/modules/c'


# ---------------------------------------------------------------------------
# TestGitSubmoduleOverlay
# ---------------------------------------------------------------------------


class TestGitSubmoduleOverlay:
    @pytest.fixture(autouse=True)
    def _setup_workspace(self, tmp_path, monkeypatch):
        """Set up a fake workspace directory with a proper .git/ structure."""
        self.root = tmp_path / 'parent'
        self.root.mkdir()
        self.root_git = self.root / '.git'
        self.root_git.mkdir()
        self.workspace = self.root / 'sub'
        self.workspace.mkdir()
        monkeypatch.chdir(self.workspace)

    def _make_submodule(self):
        """Turn self.workspace into a submodule pointing to root .git/modules/sub."""
        modules_dir = self.root_git / 'modules' / 'sub'
        modules_dir.mkdir(parents=True)
        (self.workspace / '.git').write_text(f'gitdir: {modules_dir}\n')
        return modules_dir

    def test_normal_repo_no_git_mount(self):
        """.git is a directory → no .git mount in args."""
        (self.workspace / '.git').mkdir()
        args = _host_overlay_args({}, [])
        assert not any('.git' in a and a.startswith('-v=') for a in args)

    def test_submodule_mounts_root_git(self):
        """.git file with valid gitdir → root .git/ mounted."""
        self._make_submodule()
        args = _host_overlay_args({}, [])
        # Default workspace is /app, depth=1 → mount at /.git
        assert f'-v={self.root_git}:/.git:z' in args

    def test_submodule_no_env_vars(self):
        """Submodule mount emits no GIT_DIR/GIT_WORK_TREE/GIT_CEILING_DIRECTORIES."""
        self._make_submodule()
        args = _host_overlay_args({}, [])
        assert not any('--env=GIT_DIR' in a for a in args)
        assert not any('--env=GIT_WORK_TREE' in a for a in args)
        assert not any('--env=GIT_CEILING' in a for a in args)

    def test_submodule_only_one_arg(self):
        """Submodule detection emits exactly one arg (the mount)."""
        self._make_submodule()
        args = _host_overlay_args({}, [])
        git_args = [a for a in args if '.git' in a and a.startswith('-v=')]
        assert len(git_args) == 1

    def test_broken_gitdir_pointer_skipped(self):
        """.git file pointing to nonexistent path → no mount."""
        (self.workspace / '.git').write_text('gitdir: /nonexistent/path\n')
        args = _host_overlay_args({}, [])
        assert not any('.git' in a and a.startswith('-v=') for a in args)

    def test_no_dot_git_skipped(self):
        """No .git at all → no mount."""
        (self.workspace / '.git').unlink(missing_ok=True)
        args = _host_overlay_args({}, [])
        assert not any('.git' in a and a.startswith('-v=') for a in args)

    def test_non_gitdir_file_skipped(self):
        """.git file without gitdir: prefix → no mount."""
        (self.workspace / '.git').write_text('random content\n')
        args = _host_overlay_args({}, [])
        assert not any('.git' in a and a.startswith('-v=') for a in args)

    def test_workspace_mount_submodule_via_dc(self, tmp_path):
        """devcontainer_run_args: workspaceMount source has .git pointer → root git mounted."""
        host_src = tmp_path / 'dc_parent' / 'sub'
        host_src.mkdir(parents=True)
        root_git = tmp_path / 'dc_parent' / '.git'
        root_git.mkdir()
        modules_dir = root_git / 'modules' / 'sub'
        modules_dir.mkdir(parents=True)
        (host_src / '.git').write_text(f'gitdir: {modules_dir}\n')
        dc = {
            'workspaceMount': f'source={host_src},target=/workspace,type=bind',
            'workspaceFolder': '/workspace',
        }
        args = devcontainer_run_args(dc, {})
        # workspace=/workspace, depth=1 → mount at /.git
        assert f'-v={root_git}:/.git:z' in args
        assert not any('--env=GIT_DIR' in a for a in args)

    def test_workspace_mount_normal_repo_via_dc(self, tmp_path):
        """devcontainer_run_args: workspaceMount source has .git directory → no mount."""
        host_src = tmp_path / 'host_project'
        host_src.mkdir()
        (host_src / '.git').mkdir()
        dc = {
            'workspaceMount': f'source={host_src},target=/workspace,type=bind',
            'workspaceFolder': '/workspace',
        }
        args = devcontainer_run_args(dc, {})
        assert not any('.git' in a and a.startswith('-v=') for a in args)

    def test_no_auto_resolve_flag_skips_host_overlay(self):
        """--no-auto-resolve-git-submodules prevents mount in host overlay."""
        self._make_submodule()
        args = _host_overlay_args({'run.no_auto_resolve_git_submodules': True}, [])
        assert not any('.git' in a and a.startswith('-v=') for a in args)

    def test_no_auto_resolve_flag_skips_dc(self, tmp_path):
        """--no-auto-resolve-git-submodules prevents mount in devcontainer_run_args."""
        host_src = tmp_path / 'dc_parent' / 'sub'
        host_src.mkdir(parents=True)
        root_git = tmp_path / 'dc_parent' / '.git'
        root_git.mkdir()
        modules_dir = root_git / 'modules' / 'sub'
        modules_dir.mkdir(parents=True)
        (host_src / '.git').write_text(f'gitdir: {modules_dir}\n')
        dc = {
            'workspaceMount': f'source={host_src},target=/workspace,type=bind',
            'workspaceFolder': '/workspace',
        }
        args = devcontainer_run_args(dc, {'run.no_auto_resolve_git_submodules': True})
        assert not any('.git' in a and a.startswith('-v=') for a in args)


# ---------------------------------------------------------------------------
# _git_submodule_args
# ---------------------------------------------------------------------------


class TestGitSubmoduleArgs:
    def test_shallow_workspace_mounts_at_root(self, tmp_path):
        """Workspace at /app (depth 1), submod depth 1 → mount at /.git."""
        root_git = tmp_path / 'parent' / '.git'
        modules_dir = root_git / 'modules' / 'sub'
        modules_dir.mkdir(parents=True)
        workspace = tmp_path / 'parent' / 'sub'
        workspace.mkdir()
        (workspace / '.git').write_text(f'gitdir: {modules_dir}\n')
        args = _git_submodule_args(str(workspace), '/app')
        assert args == [f'-v={root_git}:/.git:z']

    def test_deep_workspace_mounts_relative(self, tmp_path):
        """Workspace at /a/b/c/d/e (depth 5), submod depth 2 → mount at /a/b/c/.git."""
        root_git = tmp_path / 'parent' / '.git'
        modules_dir = root_git / 'modules' / 'x' / 'y'
        modules_dir.mkdir(parents=True)
        workspace = tmp_path / 'parent' / 'x' / 'y'
        workspace.mkdir(parents=True)
        (workspace / '.git').write_text(f'gitdir: {modules_dir}\n')
        args = _git_submodule_args(str(workspace), '/a/b/c/d/e')
        # depth=2 (x/y), walk up /a/b/c/d/e by 2 → /a/b/c
        assert args == [f'-v={root_git}:/a/b/c/.git:z']

    def test_deep_workspace_shallow_submod_mounts_near_root(self, tmp_path):
        """Workspace at /a/b/c (depth 3), submod depth 3 → mount at /.git."""
        root_git = tmp_path / 'parent' / '.git'
        modules_dir = root_git / 'modules' / 'x' / 'y' / 'z'
        modules_dir.mkdir(parents=True)
        workspace = tmp_path / 'parent' / 'x' / 'y' / 'z'
        workspace.mkdir(parents=True)
        (workspace / '.git').write_text(f'gitdir: {modules_dir}\n')
        args = _git_submodule_args(str(workspace), '/a/b/c')
        # depth=3, walk up /a/b/c by 3 → /
        assert args == [f'-v={root_git}:/.git:z']

    def test_submod_depth_exceeds_workspace_clamps_at_root(self, tmp_path):
        """Submod depth > workspace depth → clamps at /.git (POSIX /../ at / = /)."""
        root_git = tmp_path / 'parent' / '.git'
        modules_dir = root_git / 'modules' / 'a' / 'b' / 'c'
        modules_dir.mkdir(parents=True)
        workspace = tmp_path / 'parent' / 'a' / 'b' / 'c'
        workspace.mkdir(parents=True)
        (workspace / '.git').write_text(f'gitdir: {modules_dir}\n')
        args = _git_submodule_args(str(workspace), '/w')
        # depth=3, walk up /w by 3 → / (PurePosixPath.parent stops at /)
        assert args == [f'-v={root_git}:/.git:z']

    def test_normal_repo_returns_empty(self, tmp_path):
        workspace = tmp_path / 'project'
        workspace.mkdir()
        (workspace / '.git').mkdir()
        assert _git_submodule_args(str(workspace), '/app') == []

    def test_broken_pointer_returns_empty(self, tmp_path):
        workspace = tmp_path / 'project'
        workspace.mkdir()
        (workspace / '.git').write_text('gitdir: /nonexistent/path\n')
        assert _git_submodule_args(str(workspace), '/app') == []

    def test_no_dot_git_returns_empty(self, tmp_path):
        workspace = tmp_path / 'project'
        workspace.mkdir()
        assert _git_submodule_args(str(workspace), '/app') == []
