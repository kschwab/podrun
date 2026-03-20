"""Tests for Phase 2.2 — entrypoint generation (run, rc, exec)."""

import os
import stat

import pytest

import podrun.podrun as podrun_mod
from podrun.podrun import (
    BOOTSTRAP_CAPS,
    GID,
    PODRUN_RC_PATH,
    PODRUN_READY_PATH,
    UID,
    UNAME,
    __version__,
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
        """Copy-staging loop chowns copied files to container user."""
        path = generate_run_entrypoint(_default_ns())
        with open(path) as f:
            content = f.read()
        # Find the chown inside the copy-staging block
        staging_section = content[content.index('Copy-mode staging') :]
        assert f'chown -R {UID}:{GID}' in staging_section


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
        assert '_prompt_banner="podrun"' in content

    def test_default_prompt_banner_from_image(self):
        """With no prompt_banner set, falls back to image name."""
        path = generate_rc_sh(_default_ns(**{'run.image': 'alpine:3.18'}))
        with open(path) as f:
            content = f.read()
        assert '_prompt_banner="alpine:3.18"' in content

    def test_custom_prompt_banner(self):
        path = generate_rc_sh(_default_ns(**{'run.prompt_banner': 'myproject'}))
        with open(path) as f:
            content = f.read()
        assert '_prompt_banner="myproject"' in content

    def test_custom_prompt_banner_overrides_image(self):
        """Explicit prompt_banner takes priority over image name."""
        path = generate_rc_sh(
            _default_ns(**{'run.prompt_banner': 'myproject', 'run.image': 'alpine:3.18'})
        )
        with open(path) as f:
            content = f.read()
        assert '_prompt_banner="myproject"' in content

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

    def test_rc_sh_missing_keys_use_defaults(self):
        path = generate_rc_sh({})
        with open(path) as f:
            content = f.read()
        assert '_prompt_banner="podrun"' in content
