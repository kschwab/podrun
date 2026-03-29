"""Tests for Phase 2.4 — container state + command assembly."""

import subprocess

import pytest

import podrun.podrun as podrun_mod
from podrun.podrun import (
    BOOTSTRAP_CAPS,
    PODRUN_ENTRYPOINT_PATH,
    PODRUN_EXEC_ENTRY_PATH,
    PODRUN_RC_PATH,
    PodrunContext,
    build_overlay_run_command,
    build_podman_exec_args,
    detect_container_state,
    handle_container_state,
    parse_args,
    query_container_info,
    resolve_config,
)


pytestmark = pytest.mark.usefixtures('podman_binary')


def _ctx_from_ns(ns, **kwargs):
    """Build a minimal PodrunContext from an ns dict for unit tests."""
    return PodrunContext(
        ns=ns,
        trailing_args=kwargs.get('trailing_args', []),
        explicit_command=kwargs.get('explicit_command', []),
        raw_argv=kwargs.get('raw_argv', []),
        subcmd_passthrough_args=kwargs.get('subcmd_passthrough_args', []),
        podman_path=kwargs.get('podman_path', 'podman'),
    )


# ---------------------------------------------------------------------------
# detect_container_state
# ---------------------------------------------------------------------------


class TestDetectContainerState:
    def test_empty_name_returns_none(self):
        assert detect_container_state('') is None
        assert detect_container_state(None) is None

    def test_running(self, monkeypatch):
        monkeypatch.setattr(
            podrun_mod,
            'run_os_cmd',
            lambda cmd: subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='running',
                stderr='',
            ),
        )
        assert detect_container_state('mycontainer') == 'running'

    def test_exited(self, monkeypatch):
        monkeypatch.setattr(
            podrun_mod,
            'run_os_cmd',
            lambda cmd: subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='exited',
                stderr='',
            ),
        )
        assert detect_container_state('mycontainer') == 'stopped'

    def test_created(self, monkeypatch):
        monkeypatch.setattr(
            podrun_mod,
            'run_os_cmd',
            lambda cmd: subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='created',
                stderr='',
            ),
        )
        assert detect_container_state('mycontainer') == 'stopped'

    def test_paused(self, monkeypatch):
        monkeypatch.setattr(
            podrun_mod,
            'run_os_cmd',
            lambda cmd: subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='paused',
                stderr='',
            ),
        )
        assert detect_container_state('mycontainer') == 'stopped'

    def test_dead(self, monkeypatch):
        monkeypatch.setattr(
            podrun_mod,
            'run_os_cmd',
            lambda cmd: subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='dead',
                stderr='',
            ),
        )
        assert detect_container_state('mycontainer') == 'stopped'

    def test_inspect_fails(self, monkeypatch):
        monkeypatch.setattr(
            podrun_mod,
            'run_os_cmd',
            lambda cmd: subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout='',
                stderr='Error',
            ),
        )
        assert detect_container_state('mycontainer') is None

    def test_unknown_status(self, monkeypatch):
        monkeypatch.setattr(
            podrun_mod,
            'run_os_cmd',
            lambda cmd: subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='removing',
                stderr='',
            ),
        )
        assert detect_container_state('mycontainer') is None

    def test_global_flags_in_command(self, monkeypatch):
        captured = {}

        def fake_run(cmd):
            captured['cmd'] = cmd
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout='running', stderr='')

        monkeypatch.setattr(podrun_mod, 'run_os_cmd', fake_run)
        detect_container_state('myc', global_flags=['--root=/tmp/root'])
        assert '--root=/tmp/root' in captured['cmd']

    def test_custom_podman_path(self, monkeypatch):
        captured = {}

        def fake_run(cmd):
            captured['cmd'] = cmd
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout='running', stderr='')

        monkeypatch.setattr(podrun_mod, 'run_os_cmd', fake_run)
        detect_container_state('myc', podman_path='/usr/local/bin/podman')
        assert '/usr/local/bin/podman' in captured['cmd']

    def test_whitespace_stripped(self, monkeypatch):
        monkeypatch.setattr(
            podrun_mod,
            'run_os_cmd',
            lambda cmd: subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='  running  \n',
                stderr='',
            ),
        )
        assert detect_container_state('mycontainer') == 'running'


# ---------------------------------------------------------------------------
# handle_container_state
# ---------------------------------------------------------------------------


class TestHandleContainerState:
    def test_no_name_returns_run(self):
        assert handle_container_state(_ctx_from_ns({})) == 'run'

    def test_container_not_found_returns_run(self, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'detect_container_state', lambda *a, **kw: None)
        assert handle_container_state(_ctx_from_ns({'run.name': 'myc'})) == 'run'

    def test_running_auto_attach(self, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'detect_container_state', lambda *a, **kw: 'running')
        ns = {'run.name': 'myc', 'run.auto_attach': True}
        assert handle_container_state(_ctx_from_ns(ns)) == 'attach'

    def test_running_auto_replace(self, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'detect_container_state', lambda *a, **kw: 'running')
        ns = {'run.name': 'myc', 'run.auto_replace': True}
        assert handle_container_state(_ctx_from_ns(ns)) == 'replace'

    def test_running_both_false_returns_none(self, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'detect_container_state', lambda *a, **kw: 'running')
        ns = {'run.name': 'myc', 'run.auto_attach': False, 'run.auto_replace': False}
        assert handle_container_state(_ctx_from_ns(ns)) is None

    def test_running_prompt_attach_yes(self, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'detect_container_state', lambda *a, **kw: 'running')
        monkeypatch.setattr(podrun_mod, 'yes_no_prompt', lambda msg, default, interactive: True)
        ns = {'run.name': 'myc'}
        assert handle_container_state(_ctx_from_ns(ns)) == 'attach'

    def test_running_prompt_attach_no_replace_yes(self, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'detect_container_state', lambda *a, **kw: 'running')
        prompts = iter([False, True])
        monkeypatch.setattr(
            podrun_mod, 'yes_no_prompt', lambda msg, default, interactive: next(prompts)
        )
        ns = {'run.name': 'myc'}
        assert handle_container_state(_ctx_from_ns(ns)) == 'replace'

    def test_running_prompt_both_no(self, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'detect_container_state', lambda *a, **kw: 'running')
        monkeypatch.setattr(podrun_mod, 'yes_no_prompt', lambda msg, default, interactive: False)
        ns = {'run.name': 'myc'}
        assert handle_container_state(_ctx_from_ns(ns)) is None

    def test_stopped_auto_attach_restarts(self, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'detect_container_state', lambda *a, **kw: 'stopped')
        # auto_attach=True + stopped → restart (auto_attach takes priority)
        ns = {'run.name': 'myc', 'run.auto_attach': True, 'run.auto_replace': True}
        assert handle_container_state(_ctx_from_ns(ns)) == 'restart'

    def test_stopped_auto_replace(self, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'detect_container_state', lambda *a, **kw: 'stopped')
        ns = {'run.name': 'myc', 'run.auto_replace': True}
        assert handle_container_state(_ctx_from_ns(ns)) == 'replace'

    def test_stopped_both_false_non_interactive(self, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'detect_container_state', lambda *a, **kw: 'stopped')
        monkeypatch.setattr('sys.stdin', type('F', (), {'isatty': lambda self: False})())
        ns = {'run.name': 'myc', 'run.auto_attach': False, 'run.auto_replace': False}
        assert handle_container_state(_ctx_from_ns(ns)) is None

    def test_stopped_prompt_restart_yes(self, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'detect_container_state', lambda *a, **kw: 'stopped')
        monkeypatch.setattr(podrun_mod, 'yes_no_prompt', lambda msg, default, interactive: True)
        ns = {'run.name': 'myc'}
        assert handle_container_state(_ctx_from_ns(ns)) == 'restart'

    def test_stopped_prompt_both_no(self, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'detect_container_state', lambda *a, **kw: 'stopped')
        monkeypatch.setattr(podrun_mod, 'yes_no_prompt', lambda msg, default, interactive: False)
        ns = {'run.name': 'myc'}
        assert handle_container_state(_ctx_from_ns(ns)) is None

    def test_stopped_prompt_restart_no_replace_yes(self, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'detect_container_state', lambda *a, **kw: 'stopped')
        prompts = iter([False, True])
        monkeypatch.setattr(
            podrun_mod, 'yes_no_prompt', lambda msg, default, interactive: next(prompts)
        )
        ns = {'run.name': 'myc'}
        assert handle_container_state(_ctx_from_ns(ns)) == 'replace'

    def test_stopped_auto_attach_priority_over_auto_replace(self, monkeypatch):
        """When both auto_attach and auto_replace are True and stopped, restart wins."""
        monkeypatch.setattr(podrun_mod, 'detect_container_state', lambda *a, **kw: 'stopped')
        ns = {'run.name': 'myc', 'run.auto_attach': True, 'run.auto_replace': True}
        assert handle_container_state(_ctx_from_ns(ns)) == 'restart'

    def test_auto_attach_priority_over_auto_replace(self, monkeypatch):
        """When both auto_attach and auto_replace are True and running, attach wins."""
        monkeypatch.setattr(podrun_mod, 'detect_container_state', lambda *a, **kw: 'running')
        ns = {'run.name': 'myc', 'run.auto_attach': True, 'run.auto_replace': True}
        assert handle_container_state(_ctx_from_ns(ns)) == 'attach'


# ---------------------------------------------------------------------------
# query_container_info
# ---------------------------------------------------------------------------


class TestQueryContainerInfo:
    def test_extracts_workdir_and_overlays(self, monkeypatch):
        stdout = 'FOO=bar\nPODRUN_WORKDIR=/work\nPODRUN_OVERLAYS=user,host\nBAZ=qux\n'
        monkeypatch.setattr(
            podrun_mod,
            'run_os_cmd',
            lambda cmd: subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=stdout,
                stderr='',
            ),
        )
        workdir, overlays = query_container_info('myc')
        assert workdir == '/work'
        assert overlays == 'user,host'

    def test_missing_vars_return_empty(self, monkeypatch):
        monkeypatch.setattr(
            podrun_mod,
            'run_os_cmd',
            lambda cmd: subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='FOO=bar\n',
                stderr='',
            ),
        )
        workdir, overlays = query_container_info('myc')
        assert workdir == ''
        assert overlays == ''

    def test_inspect_fails(self, monkeypatch):
        monkeypatch.setattr(
            podrun_mod,
            'run_os_cmd',
            lambda cmd: subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout='',
                stderr='Error',
            ),
        )
        workdir, overlays = query_container_info('myc')
        assert workdir == ''
        assert overlays == ''

    def test_global_flags_in_command(self, monkeypatch):
        captured = {}

        def fake_run(cmd):
            captured['cmd'] = cmd
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout='', stderr='')

        monkeypatch.setattr(podrun_mod, 'run_os_cmd', fake_run)
        query_container_info('myc', global_flags=['--root=/tmp/root'])
        assert '--root=/tmp/root' in captured['cmd']

    def test_workdir_with_equals(self, monkeypatch):
        """PODRUN_WORKDIR value may contain '='."""
        stdout = 'PODRUN_WORKDIR=/work=space\n'
        monkeypatch.setattr(
            podrun_mod,
            'run_os_cmd',
            lambda cmd: subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=stdout,
                stderr='',
            ),
        )
        workdir, _ = query_container_info('myc')
        assert workdir == '/work=space'


# ---------------------------------------------------------------------------
# build_podman_exec_args
# ---------------------------------------------------------------------------


class TestBuildPodmanExecArgs:
    def test_basic_structure(self):
        args = build_podman_exec_args({}, 'myc')
        assert args[0] == 'exec'
        assert '-it' in args
        assert '--detach-keys=ctrl-q,ctrl-q' in args
        assert 'myc' in args

    def test_container_workdir(self):
        args = build_podman_exec_args({}, 'myc', container_workdir='/work')
        assert '-w=/work' in args

    def test_no_workdir_when_empty(self):
        args = build_podman_exec_args({}, 'myc', container_workdir='')
        assert not any(a.startswith('-w=') for a in args)

    def test_stty_init(self):
        args = build_podman_exec_args({}, 'myc')
        stty = [a for a in args if 'PODRUN_STTY_INIT' in a]
        assert len(stty) == 1
        assert 'rows' in stty[0]
        assert 'cols' in stty[0]

    def test_stty_init_oserror(self):
        """Terminal size lookup failure is silently ignored."""
        from unittest.mock import patch

        def fail(*a, **kw):
            raise OSError('no tty')

        with patch('shutil.get_terminal_size', fail):
            args = build_podman_exec_args({}, 'myc')
        assert not any('PODRUN_STTY_INIT' in a for a in args)

    def test_env_rc_path(self):
        args = build_podman_exec_args({}, 'myc')
        assert f'-e=ENV={PODRUN_RC_PATH}' in args

    def test_shell_override(self):
        args = build_podman_exec_args({'run.shell': 'zsh'}, 'myc')
        assert '-e=PODRUN_SHELL=zsh' in args

    def test_no_shell_no_env(self):
        args = build_podman_exec_args({}, 'myc')
        assert not any('PODRUN_SHELL' in a for a in args)

    def test_login_true(self):
        args = build_podman_exec_args({'run.login': True}, 'myc')
        assert '-e=PODRUN_LOGIN=1' in args

    def test_login_false(self):
        args = build_podman_exec_args({'run.login': False}, 'myc')
        assert '-e=PODRUN_LOGIN=0' in args

    def test_login_none_no_env(self):
        args = build_podman_exec_args({'run.login': None}, 'myc')
        assert not any('PODRUN_LOGIN' in a for a in args)

    def test_interactive_session_uses_exec_entry(self):
        """No command → delegate to exec-entrypoint.sh."""
        args = build_podman_exec_args({}, 'myc')
        assert args[-1] == PODRUN_EXEC_ENTRY_PATH

    def test_explicit_command(self):
        """Explicit command after '--' bypasses exec-entrypoint."""
        args = build_podman_exec_args({}, 'myc', explicit_command=['ls', '-la'])
        assert args[-2:] == ['ls', '-la']
        assert PODRUN_EXEC_ENTRY_PATH not in args

    def test_trailing_command(self):
        """Command from trailing_args (after image) bypasses exec-entrypoint."""
        args = build_podman_exec_args({}, 'myc', trailing_args=['alpine', 'echo', 'hi'])
        assert args[-2:] == ['echo', 'hi']
        assert PODRUN_EXEC_ENTRY_PATH not in args

    def test_trailing_image_only_uses_exec_entry(self):
        """Only image in trailing_args → interactive session."""
        args = build_podman_exec_args({}, 'myc', trailing_args=['alpine'])
        assert args[-1] == PODRUN_EXEC_ENTRY_PATH

    def test_explicit_command_priority(self):
        """explicit_command takes priority over trailing_args command."""
        args = build_podman_exec_args(
            {},
            'myc',
            trailing_args=['alpine', 'echo', 'hi'],
            explicit_command=['bash'],
        )
        assert args[-1] == 'bash'
        assert 'echo' not in args

    def test_name_position(self):
        """Container name should come after options, before command."""
        args = build_podman_exec_args({}, 'myc')
        name_idx = args.index('myc')
        # Name should be after all -e= and -w= flags
        for i, a in enumerate(args):
            if a.startswith('-'):
                assert i < name_idx


# ---------------------------------------------------------------------------
# build_overlay_run_command
# ---------------------------------------------------------------------------


class TestBuildOverlayRunCommand:
    def _parse_and_resolve(self, argv):
        r = parse_args(argv)
        r = resolve_config(r)
        return r

    def test_bare_run_no_overlays(self):
        r = self._parse_and_resolve(['run', 'alpine'])
        cmd, caps = build_overlay_run_command(r)
        assert cmd[0] == 'podman'
        assert 'run' in cmd
        assert 'alpine' in cmd
        assert caps == []

    def test_user_overlay_injects_userns(self):
        r = self._parse_and_resolve(['run', '--user-overlay', 'alpine'])
        cmd, caps = build_overlay_run_command(r)
        assert '--userns=keep-id' in cmd
        assert any(a.startswith('--entrypoint=') for a in cmd)

    def test_user_overlay_returns_caps(self):
        r = self._parse_and_resolve(['run', '--user-overlay', 'alpine'])
        _, caps = build_overlay_run_command(r)
        assert caps == sorted(BOOTSTRAP_CAPS)

    def test_host_overlay_injects_network(self):
        r = self._parse_and_resolve(['run', '--host-overlay', 'alpine'])
        cmd, _ = build_overlay_run_command(r)
        assert '--network=host' in cmd
        # host implies user
        assert '--userns=keep-id' in cmd

    def test_interactive_overlay_injects_it(self):
        r = self._parse_and_resolve(['run', '--interactive-overlay', 'alpine'])
        cmd, _ = build_overlay_run_command(r)
        assert '-it' in cmd

    def test_adhoc_adds_rm(self):
        r = self._parse_and_resolve(['run', '--adhoc', 'alpine'])
        cmd, _ = build_overlay_run_command(r)
        assert '--rm' in cmd

    def test_adhoc_no_duplicate_rm(self):
        r = self._parse_and_resolve(['run', '--adhoc', '--rm', 'alpine'])
        cmd, _ = build_overlay_run_command(r)
        assert cmd.count('--rm') == 1

    def test_name_in_command(self):
        r = self._parse_and_resolve(['run', '--name=myc', 'alpine'])
        cmd, _ = build_overlay_run_command(r)
        assert '--name=myc' in cmd

    def test_env_args_always_present(self):
        r = self._parse_and_resolve(['run', '--user-overlay', 'alpine'])
        cmd, _ = build_overlay_run_command(r)
        assert any('PODRUN_OVERLAYS=' in a for a in cmd)

    def test_alt_entrypoint_extraction(self):
        r = self._parse_and_resolve(['run', '--user-overlay', '--entrypoint=/custom/ep', 'alpine'])
        cmd, _ = build_overlay_run_command(r)
        # Alt entrypoint should be extracted and passed as env
        assert any('PODRUN_ALT_ENTRYPOINT=/custom/ep' in a for a in cmd)
        # The podrun entrypoint should be the actual --entrypoint
        assert f'--entrypoint={PODRUN_ENTRYPOINT_PATH}' in cmd

    def test_no_alt_entrypoint_without_user_overlay(self):
        r = self._parse_and_resolve(['run', '--entrypoint=/custom/ep', 'alpine'])
        cmd, _ = build_overlay_run_command(r)
        # Without user overlay, entrypoint stays in passthrough (space-separated by _PassthroughAction)
        ep_idx = cmd.index('--entrypoint')
        assert cmd[ep_idx + 1] == '/custom/ep'
        assert not any('PODRUN_ALT_ENTRYPOINT' in a for a in cmd)

    def test_tilde_expansion_with_user_overlay(self):
        r = self._parse_and_resolve(['run', '--user-overlay', '-v=~/src:/dst', 'alpine'])
        cmd, _ = build_overlay_run_command(r)
        # _PassthroughAction stores as ['-v', '~/src:/dst'] (space form)
        # After tilde expansion, ~ should be resolved
        for i, a in enumerate(cmd):
            if a == '-v' and i + 1 < len(cmd) and '/dst' in cmd[i + 1]:
                assert '~' not in cmd[i + 1]
                break
            elif a.startswith('-v=') and '/dst' in a:
                assert '~' not in a
                break
        else:
            pytest.fail('No volume mount with /dst found in cmd')

    def test_explicit_command_preserved(self):
        r = self._parse_and_resolve(['run', 'alpine', '--', 'echo', 'hi'])
        cmd, _ = build_overlay_run_command(r)
        assert '--' in cmd
        sep_idx = cmd.index('--')
        assert cmd[sep_idx + 1 :] == ['echo', 'hi']

    def test_privileged_drops_no_caps(self):
        r = self._parse_and_resolve(['run', '--user-overlay', '--privileged', 'alpine'])
        _, caps = build_overlay_run_command(r)
        assert caps == []

    def test_cap_add_filters_caps(self):
        r = self._parse_and_resolve(['run', '--user-overlay', '--cap-add=CAP_CHOWN', 'alpine'])
        _, caps = build_overlay_run_command(r)
        assert 'CAP_CHOWN' not in caps

    def test_dotfiles_overlay(self, tmp_path, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'USER_HOME', str(tmp_path))
        monkeypatch.setenv('HOME', str(tmp_path))
        (tmp_path / '.vimrc').write_text('set nocp')
        r = self._parse_and_resolve(['run', '--dotfiles', 'alpine'])
        cmd, _ = build_overlay_run_command(r)
        assert any('.vimrc' in a for a in cmd)

    def test_print_cmd_format(self):
        """Command should be a list of strings suitable for shlex.join."""
        r = self._parse_and_resolve(['run', '--user-overlay', 'alpine'])
        cmd, _ = build_overlay_run_command(r)
        assert all(isinstance(a, str) for a in cmd)

    def test_x11_not_added_by_default(self):
        r = self._parse_and_resolve(['run', 'alpine'])
        cmd, _ = build_overlay_run_command(r)
        assert not any('DISPLAY' in a for a in cmd)

    def test_label_in_command(self):
        r = self._parse_and_resolve(['run', '--label=env=prod', 'alpine'])
        cmd, _ = build_overlay_run_command(r)
        assert '--label=env=prod' in cmd
