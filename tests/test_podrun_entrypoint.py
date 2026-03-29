"""Tests for Phase 2.2 — entrypoint generation (run, rc, exec)."""

import os
import stat

import pytest

import podrun.podrun as podrun_mod
from podrun.podrun import (
    BOOTSTRAP_CAPS,
    ENV_PODRUN_DEVCONTAINER_CLI,
    GID,
    PODRUN_RC_PATH,
    PODRUN_READY_PATH,
    UID,
    UNAME,
    __version__,
    _lifecycle_command_to_shell,
    _run_initialize_command,
    generate_exec_entrypoint,
    generate_rc_sh,
    generate_run_entrypoint,
)


def _default_ns(**overrides):
    """Build a minimal ns dict for entrypoint generation."""
    ns = {
        'run.login': None,
        'run.shell': None,
        'run.export': [],
        'run.prompt_banner': None,
    }
    ns.update(overrides)
    return ns


# ---------------------------------------------------------------------------
# generate_run_entrypoint
# ---------------------------------------------------------------------------


class TestGenerateRunEntrypoint:
    def test_returns_file_path(self):
        path = generate_run_entrypoint(_default_ns())
        assert os.path.isfile(path)

    def test_executable(self):
        path = generate_run_entrypoint(_default_ns())
        assert os.stat(path).st_mode & stat.S_IXUSR

    def test_shebang_no_login(self):
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            first_line = f.readline()
        assert first_line.strip() == '#!/bin/sh'

    def test_shebang_login(self):
        path = generate_run_entrypoint(_default_ns(**{'run.login': True}))
        with open(path) as f:
            first_line = f.readline()
        assert first_line.strip() == '#!/bin/sh -l'

    def test_version_comment(self):
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            content = f.read()
        assert f'podrun {__version__}' in content

    def test_set_e(self):
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            content = f.read()
        assert 'set -e' in content

    def test_username_in_script(self):
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            content = f.read()
        assert UNAME in content

    def test_uid_gid_in_script(self):
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            content = f.read()
        assert str(UID) in content
        assert str(GID) in content

    def test_uid_collision_removal(self):
        """Entrypoint removes /etc/passwd entries with same UID but different username."""
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            content = f.read()
        # passwd: delete lines NOT starting with our username that have our UID
        assert f'/^{UNAME}:/!' in content
        assert f'/^[^:]*:[^:]*:{UID}:/d' in content
        # group: delete lines NOT starting with our username that have our GID
        assert f'/^[^:]*:[^:]*:[^:]*:{GID}:/d' in content

    def test_passwd_entry_fallback(self):
        """Entrypoint adds passwd entry as fallback when --passwd-entry was ignored."""
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            content = f.read()
        # awk check for UID existence + echo fallback with $SHELL
        assert f'-v uid={UID}' in content
        assert f'{UNAME}:*:{UID}:{GID}:{UNAME}:/home/{UNAME}:$SHELL' in content

    def test_home_dir_creation(self):
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            content = f.read()
        assert f'mkdir -p /home/{UNAME}' in content

    def test_skel_copy(self):
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            content = f.read()
        assert '/etc/skel' in content

    def test_skel_skips_bind_mounts(self):
        """Skel copy skips entries whose destination is a bind mount (different device)."""
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            content = f.read()
        assert 'stat -c %d' in content
        assert '_home_dev' in content
        assert '_dest_dev' in content
        assert '[ "$_dest_dev" != "$_home_dev" ] && continue' in content

    def test_skel_glob_pattern(self):
        """Skel copy uses three-pattern glob to match all entries including hidden files."""
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            content = f.read()
        assert '/etc/skel/*' in content
        assert '/etc/skel/.[!.]*' in content
        assert '/etc/skel/..?*' in content

    def test_sudo_setup(self):
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            content = f.read()
        assert 'NOPASSWD:ALL' in content

    def test_bashrc_wiring(self):
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            content = f.read()
        assert PODRUN_RC_PATH in content

    def test_ready_sentinel(self):
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            content = f.read()
        assert f'touch {PODRUN_READY_PATH}' in content
        # Also verify it's inside the guard (detailed tests in TestRestartGuard)
        guard_open = content.index('# --- First-run setup')
        guard_close = content.index('# --- End first-run setup ---')
        touch_idx = content.index(f'touch {PODRUN_READY_PATH}')
        assert guard_open < touch_idx < guard_close

    def test_git_submodule_worktree_bridge(self):
        """Entrypoint derives submodule path from .git file and creates worktree symlink."""
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            content = f.read()
        # Self-contained: reads $PWD/.git, resolves mount location, creates symlink
        assert '[ -f "$PWD/.git" ]' in content
        assert '.git/modules/' in content
        assert 'cd "$PWD/$_git_prefix"' in content
        assert 'ln -sfn "$PWD" "$_git_parent/$_submod_path"' in content

    def test_alt_entrypoint_handling(self):
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            content = f.read()
        assert 'PODRUN_ALT_ENTRYPOINT' in content

    def test_shell_detect_default(self):
        """Without run.shell, script auto-detects (prefers bash over sh)."""
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            content = f.read()
        assert 'Detect shell' in content
        assert 'command -v bash' in content

    def test_shell_detect_configured(self):
        """With run.shell set, script uses that specific shell."""
        path = generate_run_entrypoint(_default_ns(**{'run.shell': 'zsh'}))
        with open(path) as f:
            content = f.read()
        assert 'command -v zsh' in content
        assert 'Use configured default shell' in content

    def test_cap_drop_setpriv(self):
        """Script should include setpriv-based cap dropping."""
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            content = f.read()
        assert 'setpriv' in content
        # Check that bootstrap caps are referenced (lowercased)
        for cap in BOOTSTRAP_CAPS:
            cap_lower = cap[4:].lower() if cap.startswith('CAP_') else cap.lower()
            assert cap_lower in content

    def test_cap_drop_capsh_fallback(self):
        """Script should include capsh fallback."""
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            content = f.read()
        assert 'capsh' in content

    def test_no_exports_no_export_block(self):
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            content = f.read()
        assert '# Export' not in content

    def test_export_strict_mode(self):
        path = generate_run_entrypoint(
            _default_ns(
                **{
                    'run.export': ['/data:/host/data'],
                }
            )
        )
        with open(path) as f:
            content = f.read()
        assert '# Export (mount): /data' in content
        assert 'ln -sfn' in content
        assert 'rm -rf' in content

    def test_export_copy_only(self):
        path = generate_run_entrypoint(
            _default_ns(
                **{
                    'run.export': ['/data:/host/data:0'],
                }
            )
        )
        with open(path) as f:
            content = f.read()
        assert '# Export (copy): /data' in content
        # copy-only should NOT have rm/symlink
        lines = [ln for ln in content.split('\n') if '# Export' in ln or 'rm -rf' in ln]
        # There should be a copy export comment but no rm -rf for it
        assert any('copy' in ln for ln in lines)

    def test_multiple_exports_sorted(self):
        path = generate_run_entrypoint(
            _default_ns(
                **{
                    'run.export': ['/z:/host/z', '/a:/host/a'],
                }
            )
        )
        with open(path) as f:
            content = f.read()
        idx_a = content.index('# Export (mount): /a')
        idx_z = content.index('# Export (mount): /z')
        assert idx_a < idx_z, 'Exports should be sorted'

    def test_idempotent(self):
        ns = _default_ns()
        p1 = generate_run_entrypoint(ns)
        p2 = generate_run_entrypoint(ns)
        assert p1 == p2

    def test_different_config_different_path(self):
        p1 = generate_run_entrypoint(_default_ns())
        p2 = generate_run_entrypoint(_default_ns(**{'run.login': True}))
        assert p1 != p2

    def test_passwd_patch(self):
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            content = f.read()
        assert 'sed -i' in content
        assert '/etc/passwd' in content

    def test_group_entry(self):
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            content = f.read()
        assert '/etc/group' in content

    def test_copy_staging_loop_present(self):
        """Entrypoint includes generic copy-staging loop."""
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            content = f.read()
        assert '/.podrun/copy-staging' in content
        assert '.podrun_target' in content
        assert 'cp -a' in content

    def test_copy_staging_loop_before_sudo(self):
        """Copy-staging loop appears before sudo setup (needs home dir first)."""
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            content = f.read()
        idx_staging = content.index('copy-staging')
        idx_sudo = content.index('Opportunistic sudo')
        assert idx_staging < idx_sudo

    def test_copy_staging_loop_after_home_dir(self):
        """Copy-staging loop appears after home directory creation."""
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            content = f.read()
        idx_home = content.index(f'mkdir -p /home/{UNAME}')
        idx_staging = content.index('copy-staging')
        assert idx_home < idx_staging

    def test_copy_staging_chown(self):
        """Copy-staging loop chowns copied dirs to match source ownership."""
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            content = f.read()
        # Find the chown inside the copy-staging block
        staging_section = content[content.index('Copy-mode staging') :]
        assert 'chown $(stat -c "%u:%g"' in staging_section

    def test_copy_staging_chmod_descriptor(self):
        """Entrypoint applies .podrun_chmod when descriptor exists."""
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            content = f.read()
        staging_section = content[content.index('Copy-mode staging') :]
        assert '.podrun_chmod' in staging_section
        assert 'chmod -R' in staging_section
        assert 'chmod "$_chmod"' in staging_section


# ---------------------------------------------------------------------------
# Restart guard — first-run vs always-run partitioning
# ---------------------------------------------------------------------------


class TestRestartGuard:
    """Verify the READY sentinel guard partitions first-run setup from the shared tail."""

    @pytest.fixture(autouse=True)
    def _generate(self):
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            self.content = f.read()
        # Locate guard boundaries using comment markers.
        self.guard_open = self.content.index('# --- First-run setup')
        self.guard_close = self.content.index('# --- End first-run setup ---')

    def test_guard_structure(self):
        """Script contains guard open/close with READY sentinel check."""
        assert f'if [ ! -e {PODRUN_READY_PATH} ]; then' in self.content
        # The fi closing the guard appears before the end marker
        fi_idx = self.content.rindex('fi', self.guard_open, self.guard_close + 1)
        assert fi_idx > self.guard_open

    def test_setup_inside_guard(self):
        """Key setup operations appear between guard open and guard close."""
        guarded = self.content[self.guard_open : self.guard_close]
        # passwd/group manipulation
        assert 'sed' in guarded and '/etc/passwd' in guarded
        # home dir creation
        assert f'mkdir -p /home/{UNAME}' in guarded
        # copy-staging
        assert '/.podrun/copy-staging' in guarded
        # sudo setup
        assert 'NOPASSWD:ALL' in guarded
        # git submodule bridge
        assert '[ -f "$PWD/.git" ]' in guarded
        # ~/workdir symlink
        assert f'/home/{UNAME}/workdir' in guarded
        # bashrc wiring
        assert PODRUN_RC_PATH in guarded

    def test_ready_touch_inside_guard(self):
        """`touch /.podrun/READY` appears between guard open and fi."""
        touch_idx = self.content.index(f'touch {PODRUN_READY_PATH}')
        assert self.guard_open < touch_idx < self.guard_close

    def test_shared_tail_outside_guard(self):
        """HOME/USER/ENV exports, alt entrypoint, and cap-drop appear after guard."""
        tail = self.content[self.guard_close :]
        # Environment exports
        assert 'HOME=/home/' in tail
        assert 'export HOME' in tail
        assert f'USER={UNAME}' in tail
        assert 'export USER' in tail
        assert f'ENV={PODRUN_RC_PATH}' in tail
        assert 'export ENV' in tail
        # Alt entrypoint handling
        assert 'PODRUN_ALT_ENTRYPOINT' in tail
        # Cap-drop exec
        assert 'setpriv' in tail or 'exec' in tail

    def test_shell_detect_before_guard(self):
        """Shell detection (PODRUN_SHELL) appears before the guard."""
        shell_idx = self.content.index('PODRUN_SHELL="$SHELL"')
        assert shell_idx < self.guard_open

    def test_exports_inside_guard(self):
        """When exports are configured, export blocks appear inside the guard."""
        path = generate_run_entrypoint(_default_ns(**{'run.export': ['/data:/host/data']}))
        with open(path) as f:
            content = f.read()
        guard_open = content.index('# --- First-run setup')
        guard_close = content.index('# --- End first-run setup ---')
        guarded = content[guard_open:guard_close]
        assert '# Export (mount): /data' in guarded


# ---------------------------------------------------------------------------
# generate_rc_sh
# ---------------------------------------------------------------------------


class TestGenerateRcSh:
    @pytest.fixture(autouse=True)
    def _mock_cpu_info(self, monkeypatch):
        """Mock run_os_cmd so rc.sh generation doesn't need /proc/cpuinfo."""
        import subprocess

        def fake_run_os_cmd(cmd, env=None):
            if 'model name' in cmd:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout='Test CPU', stderr=''
                )
            if 'processor' in cmd:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout='4', stderr='')
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout='', stderr='')

        monkeypatch.setattr(podrun_mod, 'run_os_cmd', fake_run_os_cmd)

    def test_returns_file_path(self):
        path = generate_rc_sh(_default_ns())
        assert os.path.isfile(path)

    def test_version_comment(self):
        path = generate_rc_sh(_default_ns())
        with open(path) as f:
            content = f.read()
        assert f'podrun {__version__}' in content

    def test_stty_init_handling(self):
        path = generate_rc_sh(_default_ns())
        with open(path) as f:
            content = f.read()
        assert 'PODRUN_STTY_INIT' in content

    def test_default_prompt_banner_no_image(self):
        """With no prompt_banner and no image, falls back to 'podrun'."""
        path = generate_rc_sh(_default_ns())
        with open(path) as f:
            content = f.read()
        assert '_prompt_banner="podrun 📦"' in content

    def test_default_prompt_banner_from_image(self):
        """With no prompt_banner set, falls back to image name."""
        path = generate_rc_sh(_default_ns(**{'run.image': 'alpine:3.18'}))
        with open(path) as f:
            content = f.read()
        assert '_prompt_banner="alpine:3.18 📦"' in content

    def test_custom_prompt_banner(self):
        path = generate_rc_sh(_default_ns(**{'run.prompt_banner': 'myproject'}))
        with open(path) as f:
            content = f.read()
        assert '_prompt_banner="myproject 📦"' in content

    def test_custom_prompt_banner_overrides_image(self):
        """Explicit prompt_banner takes priority over image name."""
        path = generate_rc_sh(
            _default_ns(**{'run.prompt_banner': 'myproject', 'run.image': 'alpine:3.18'})
        )
        with open(path) as f:
            content = f.read()
        assert '_prompt_banner="myproject 📦"' in content

    def test_cpu_info_embedded(self):
        path = generate_rc_sh(_default_ns())
        with open(path) as f:
            content = f.read()
        assert 'Test CPU' in content
        assert '4 vCPU' in content

    def test_ps1_set(self):
        path = generate_rc_sh(_default_ns())
        with open(path) as f:
            content = f.read()
        assert 'PS1=' in content

    def test_ascii_art(self):
        path = generate_rc_sh(_default_ns())
        with open(path) as f:
            content = f.read()
        assert 'PODRUN' in content
        assert 'EOT' in content

    def test_memory_info(self):
        path = generate_rc_sh(_default_ns())
        with open(path) as f:
            content = f.read()
        assert 'MemTotal' in content
        assert 'MemAvailable' in content

    def test_uptime_calculation(self):
        path = generate_rc_sh(_default_ns())
        with open(path) as f:
            content = f.read()
        assert '/proc/uptime' in content

    def test_idempotent(self):
        ns = _default_ns()
        p1 = generate_rc_sh(ns)
        p2 = generate_rc_sh(ns)
        assert p1 == p2

    def test_hostname_fallback(self):
        path = generate_rc_sh(_default_ns())
        with open(path) as f:
            content = f.read()
        assert 'HOSTNAME=' in content


# ---------------------------------------------------------------------------
# generate_exec_entrypoint
# ---------------------------------------------------------------------------


class TestGenerateExecEntrypoint:
    def test_returns_file_path(self):
        path = generate_exec_entrypoint()
        assert os.path.isfile(path)

    def test_executable(self):
        path = generate_exec_entrypoint()
        assert os.stat(path).st_mode & stat.S_IXUSR

    def test_shebang(self):
        path = generate_exec_entrypoint()
        with open(path) as f:
            first_line = f.readline()
        assert first_line.strip() == '#!/bin/sh'

    def test_version_comment(self):
        path = generate_exec_entrypoint()
        with open(path) as f:
            content = f.read()
        assert f'podrun {__version__}' in content

    def test_ready_wait(self):
        """Script should wait for READY sentinel."""
        path = generate_exec_entrypoint()
        with open(path) as f:
            content = f.read()
        assert PODRUN_READY_PATH in content
        assert 'sleep 0.1' in content

    def test_home_resolution(self):
        path = generate_exec_entrypoint()
        with open(path) as f:
            content = f.read()
        assert '/etc/passwd' in content
        assert 'HOME=' in content

    def test_shell_resolution_priority(self):
        """Shell resolution: $1 -> PODRUN_SHELL -> /etc/passwd -> /bin/sh."""
        path = generate_exec_entrypoint()
        with open(path) as f:
            content = f.read()
        assert 'PODRUN_SHELL' in content
        assert '/bin/sh' in content

    def test_bash_preference(self):
        path = generate_exec_entrypoint()
        with open(path) as f:
            content = f.read()
        assert 'command -v bash' in content

    def test_login_resolution(self):
        path = generate_exec_entrypoint()
        with open(path) as f:
            content = f.read()
        assert 'PODRUN_LOGIN' in content
        assert '"$SHELL" -l' in content

    def test_stty_resize(self):
        path = generate_exec_entrypoint()
        with open(path) as f:
            content = f.read()
        assert 'PODRUN_STTY_INIT' in content

    def test_no_ns_needed(self):
        """exec-entrypoint takes no arguments — config-independent."""
        # Should work with no arguments at all
        path = generate_exec_entrypoint()
        assert os.path.isfile(path)

    def test_idempotent(self):
        p1 = generate_exec_entrypoint()
        p2 = generate_exec_entrypoint()
        assert p1 == p2

    def test_exec_with_args(self):
        """Script should exec $@ when args provided after shift."""
        path = generate_exec_entrypoint()
        with open(path) as f:
            content = f.read()
        assert 'exec "$@"' in content

    def test_exec_shell_without_args(self):
        """Script should exec $SHELL when no args."""
        path = generate_exec_entrypoint()
        with open(path) as f:
            content = f.read()
        assert 'exec "$SHELL"' in content


# ---------------------------------------------------------------------------
# Signature simplification — ns dict instead of Config
# ---------------------------------------------------------------------------


class TestNsDictInterface:
    """Verify that entrypoint generators use ns dict, not Config dataclass."""

    def test_run_entrypoint_accepts_plain_dict(self):
        """generate_run_entrypoint should work with a plain dict."""
        path = generate_run_entrypoint({'run.login': False, 'run.shell': None, 'run.export': []})
        assert os.path.isfile(path)

    def test_rc_sh_accepts_plain_dict(self):
        """generate_rc_sh should work with a plain dict."""
        path = generate_rc_sh({'run.prompt_banner': None})
        assert os.path.isfile(path)

    def test_exec_entrypoint_no_args(self):
        """generate_exec_entrypoint takes no config at all."""
        path = generate_exec_entrypoint()
        assert os.path.isfile(path)

    def test_run_entrypoint_missing_keys_use_defaults(self):
        """Missing ns keys should gracefully default (None / empty)."""
        path = generate_run_entrypoint({})
        assert os.path.isfile(path)

    def test_exec_entrypoint_backward_compat(self):
        """generate_exec_entrypoint still works with no args."""
        path = generate_exec_entrypoint()
        assert os.path.isfile(path)

    def test_rc_sh_missing_keys_use_defaults(self):
        path = generate_rc_sh({})
        with open(path) as f:
            content = f.read()
        assert '_prompt_banner="podrun 📦"' in content


# ---------------------------------------------------------------------------
# _lifecycle_command_to_shell
# ---------------------------------------------------------------------------


class TestLifecycleCommandToShell:
    def test_none_returns_empty(self):
        assert _lifecycle_command_to_shell(None) == ''

    def test_empty_string_returns_empty(self):
        assert _lifecycle_command_to_shell('') == ''

    def test_false_returns_empty(self):
        assert _lifecycle_command_to_shell(False) == ''

    def test_string_command(self):
        result = _lifecycle_command_to_shell('echo hello')
        assert "/bin/sh -c 'echo hello'" in result

    def test_string_with_single_quotes(self):
        result = _lifecycle_command_to_shell("echo 'hi there'")
        assert '/bin/sh -c' in result
        # Single quotes are escaped
        assert "'\\''" in result or "\\'" in result

    def test_array_command(self):
        result = _lifecycle_command_to_shell(['npm', 'install', '--save'])
        assert 'npm' in result
        assert 'install' in result
        assert '--save' in result

    def test_array_single_element(self):
        result = _lifecycle_command_to_shell(['make'])
        assert 'make' in result

    def test_object_command(self):
        result = _lifecycle_command_to_shell(
            {
                'server': 'npm start',
                'watch': 'npm run watch',
            }
        )
        assert '# server' in result
        assert '# watch' in result
        assert ' &' in result
        assert 'wait' in result

    def test_object_with_array_sub(self):
        result = _lifecycle_command_to_shell(
            {
                'build': ['make', 'all'],
            }
        )
        assert '# build' in result
        assert 'make' in result
        assert ' &' in result
        assert 'wait' in result

    def test_empty_dict_returns_empty(self):
        assert _lifecycle_command_to_shell({}) == ''

    def test_empty_list_returns_empty(self):
        assert _lifecycle_command_to_shell([]) == ''

    def test_custom_indent(self):
        result = _lifecycle_command_to_shell('echo hello', indent='    ')
        assert result.startswith('    ')


# ---------------------------------------------------------------------------
# Lifecycle in run-entrypoint
# ---------------------------------------------------------------------------


class TestLifecycleInRunEntrypoint:
    def test_on_create_inside_ready_guard(self):
        """onCreateCommand appears inside READY guard (first-run only)."""
        ns = _default_ns(**{'dc.on_create_command': 'apt-get update'})
        path = generate_run_entrypoint(ns)
        with open(path) as f:
            content = f.read()
        guard_open = content.index('# --- First-run setup')
        guard_close = content.index('# --- End first-run setup ---')
        assert 'onCreateCommand' in content[guard_open:guard_close]
        assert 'apt-get update' in content[guard_open:guard_close]

    def test_post_create_inside_ready_guard(self):
        """postCreateCommand appears inside READY guard (first-run only)."""
        ns = _default_ns(**{'dc.post_create_command': 'npm install'})
        path = generate_run_entrypoint(ns)
        with open(path) as f:
            content = f.read()
        guard_open = content.index('# --- First-run setup')
        guard_close = content.index('# --- End first-run setup ---')
        assert 'postCreateCommand' in content[guard_open:guard_close]
        assert 'npm install' in content[guard_open:guard_close]

    def test_on_create_before_post_create(self):
        """onCreateCommand runs before postCreateCommand."""
        ns = _default_ns(
            **{
                'dc.on_create_command': 'step-one',
                'dc.post_create_command': 'step-two',
            }
        )
        path = generate_run_entrypoint(ns)
        with open(path) as f:
            content = f.read()
        idx_on = content.index('step-one')
        idx_post = content.index('step-two')
        assert idx_on < idx_post

    def test_post_start_outside_ready_guard(self):
        """postStartCommand appears outside READY guard (runs every start)."""
        ns = _default_ns(**{'dc.post_start_command': 'redis-server'})
        path = generate_run_entrypoint(ns)
        with open(path) as f:
            content = f.read()
        guard_close = content.index('# --- End first-run setup ---')
        tail = content[guard_close:]
        assert 'postStartCommand' in tail
        assert 'redis-server' in tail

    def test_devcontainer_cli_guard_present(self):
        """Lifecycle blocks are guarded by PODRUN_DEVCONTAINER_CLI."""
        ns = _default_ns(
            **{
                'dc.on_create_command': 'echo test',
                'dc.post_start_command': 'echo start',
            }
        )
        path = generate_run_entrypoint(ns)
        with open(path) as f:
            content = f.read()
        assert ENV_PODRUN_DEVCONTAINER_CLI in content

    def test_no_lifecycle_no_lifecycle_block(self):
        """Without lifecycle commands, no lifecycle blocks appear."""
        ns = _default_ns()
        path = generate_run_entrypoint(ns)
        with open(path) as f:
            content = f.read()
        assert 'Devcontainer lifecycle' not in content

    def test_lifecycle_before_ready_touch(self):
        """First-run lifecycle commands appear before touch READY."""
        ns = _default_ns(**{'dc.on_create_command': 'setup-step'})
        path = generate_run_entrypoint(ns)
        with open(path) as f:
            content = f.read()
        idx_cmd = content.index('setup-step')
        idx_touch = content.index(f'touch {PODRUN_READY_PATH}')
        assert idx_cmd < idx_touch

    def test_array_lifecycle_command(self):
        """Array-form lifecycle command is rendered."""
        ns = _default_ns(**{'dc.on_create_command': ['make', 'build']})
        path = generate_run_entrypoint(ns)
        with open(path) as f:
            content = f.read()
        assert 'make' in content
        assert 'build' in content

    def test_object_lifecycle_command(self):
        """Object-form lifecycle command has background + wait."""
        ns = _default_ns(
            **{
                'dc.post_create_command': {'web': 'npm start', 'api': 'go run .'},
            }
        )
        path = generate_run_entrypoint(ns)
        with open(path) as f:
            content = f.read()
        assert '# web' in content
        assert '# api' in content
        assert ' &' in content
        assert 'wait' in content

    def test_post_attach_outside_ready_guard(self):
        """postAttachCommand appears outside READY guard (runs every start)."""
        ns = _default_ns(**{'dc.post_attach_command': 'git fetch'})
        path = generate_run_entrypoint(ns)
        with open(path) as f:
            content = f.read()
        guard_close = content.index('# --- End first-run setup ---')
        tail = content[guard_close:]
        assert 'postAttachCommand' in tail
        assert 'git fetch' in tail

    def test_post_attach_after_post_start(self):
        """postAttachCommand runs after postStartCommand."""
        ns = _default_ns(
            **{
                'dc.post_start_command': 'start-step',
                'dc.post_attach_command': 'attach-step',
            }
        )
        path = generate_run_entrypoint(ns)
        with open(path) as f:
            content = f.read()
        idx_start = content.index('start-step')
        idx_attach = content.index('attach-step')
        assert idx_start < idx_attach

    def test_lifecycle_ok_initialized(self):
        """_PODRUN_LIFECYCLE_OK=1 is set near the top of the entrypoint."""
        ns = _default_ns()
        path = generate_run_entrypoint(ns)
        with open(path) as f:
            content = f.read()
        assert '_PODRUN_LIFECYCLE_OK=1' in content

    def test_lifecycle_ok_guard_in_block(self):
        """Lifecycle blocks check _PODRUN_LIFECYCLE_OK before running."""
        ns = _default_ns(**{'dc.post_start_command': 'redis-server'})
        path = generate_run_entrypoint(ns)
        with open(path) as f:
            content = f.read()
        assert '_PODRUN_LIFECYCLE_OK' in content
        # The guard should appear before the command
        idx_guard = content.index('_PODRUN_LIFECYCLE_OK')
        idx_cmd = content.index('redis-server')
        assert idx_guard < idx_cmd

    def test_lifecycle_fault_tolerant(self):
        """Failed lifecycle command sets flag and prints warning, doesn't abort."""
        ns = _default_ns(**{'dc.on_create_command': 'false'})
        path = generate_run_entrypoint(ns)
        with open(path) as f:
            content = f.read()
        # Subshell wrapping for fault tolerance
        assert '( ' in content or '(\n' in content
        # Warning on failure
        assert 'warning: onCreateCommand failed' in content
        # Flag gets set to 0 on failure
        assert '_PODRUN_LIFECYCLE_OK=0' in content

    def test_lifecycle_failure_skips_subsequent(self):
        """When a lifecycle command fails, subsequent ones are skipped."""
        ns = _default_ns(
            **{
                'dc.on_create_command': 'first-cmd',
                'dc.post_create_command': 'second-cmd',
                'dc.post_start_command': 'third-cmd',
                'dc.post_attach_command': 'fourth-cmd',
            }
        )
        path = generate_run_entrypoint(ns)
        with open(path) as f:
            content = f.read()
        # Each block should check _PODRUN_LIFECYCLE_OK
        # Count occurrences — should be at least 4 (one per lifecycle block)
        assert content.count('"$_PODRUN_LIFECYCLE_OK" = 1') >= 4


# ---------------------------------------------------------------------------
# Lifecycle in exec-entrypoint
# ---------------------------------------------------------------------------


class TestLifecycleInExecEntrypoint:
    def test_post_attach_in_exec_entrypoint(self):
        """postAttachCommand appears in exec-entrypoint."""
        ns = {'dc.post_attach_command': 'git fetch'}
        path = generate_exec_entrypoint(ns)
        with open(path) as f:
            content = f.read()
        assert 'postAttachCommand' in content
        assert 'git fetch' in content

    def test_post_attach_before_exec(self):
        """postAttachCommand appears before the Exec block."""
        ns = {'dc.post_attach_command': 'my-attach-cmd'}
        path = generate_exec_entrypoint(ns)
        with open(path) as f:
            content = f.read()
        idx_cmd = content.index('my-attach-cmd')
        idx_exec = content.index('# --- Exec ---')
        assert idx_cmd < idx_exec

    def test_devcontainer_cli_guard_in_exec(self):
        """postAttachCommand is guarded by PODRUN_DEVCONTAINER_CLI."""
        ns = {'dc.post_attach_command': 'echo attach'}
        path = generate_exec_entrypoint(ns)
        with open(path) as f:
            content = f.read()
        assert ENV_PODRUN_DEVCONTAINER_CLI in content

    def test_no_ns_no_lifecycle(self):
        """With no ns, no lifecycle blocks appear."""
        path = generate_exec_entrypoint()
        with open(path) as f:
            content = f.read()
        assert 'Devcontainer lifecycle' not in content

    def test_empty_ns_no_lifecycle(self):
        """With empty ns, no lifecycle blocks appear."""
        path = generate_exec_entrypoint({})
        with open(path) as f:
            content = f.read()
        assert 'Devcontainer lifecycle' not in content

    def test_backward_compat_no_args(self):
        """generate_exec_entrypoint() with no args still works."""
        path = generate_exec_entrypoint()
        assert os.path.isfile(path)


# ---------------------------------------------------------------------------
# _run_initialize_command — host-side lifecycle execution
# ---------------------------------------------------------------------------


class TestRunInitializeCommand:
    def test_none_is_noop(self):
        _run_initialize_command(None)

    def test_empty_string_is_noop(self):
        _run_initialize_command('')

    def test_false_is_noop(self):
        _run_initialize_command(False)

    def test_string_command_succeeds(self):
        _run_initialize_command('true')

    def test_string_command_failure_exits(self):
        with pytest.raises(SystemExit):
            _run_initialize_command('false')

    def test_array_command_succeeds(self):
        _run_initialize_command(['true'])

    def test_array_command_failure_exits(self):
        with pytest.raises(SystemExit):
            _run_initialize_command(['/bin/false'])

    def test_object_command_succeeds(self):
        _run_initialize_command({'a': 'true', 'b': 'true'})

    def test_object_command_partial_failure_exits(self):
        with pytest.raises(SystemExit):
            _run_initialize_command({'ok': 'true', 'fail': 'false'})

    def test_object_empty_sub_skipped(self):
        _run_initialize_command({'a': 'true', 'b': ''})

    def test_string_shell_features(self):
        """String form supports shell features like &&."""
        _run_initialize_command('true && true')

    def test_array_with_args(self):
        _run_initialize_command(['/bin/sh', '-c', 'true'])
