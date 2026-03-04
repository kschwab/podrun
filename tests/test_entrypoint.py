import os
import stat

from podrun.podrun import (
    PODRUN_READY_PATH,
    __version__,
    _write_sha_file,
    generate_run_entrypoint,
    generate_exec_entrypoint,
    generate_rc_sh,
)

from conftest import FAKE_GID, FAKE_UID, FAKE_UNAME


class TestWriteShaFile:
    def test_creates_file(self, podrun_tmp):
        path = _write_sha_file('hello', 'test_', '.txt')
        assert os.path.exists(path)
        with open(path) as f:
            assert f.read() == 'hello'

    def test_executable_bit(self, podrun_tmp):
        path = _write_sha_file('hello', 'test_', '.sh')
        mode = os.stat(path).st_mode
        assert mode & stat.S_IXUSR

    def test_idempotent(self, podrun_tmp):
        path1 = _write_sha_file('hello', 'test_', '.txt')
        path2 = _write_sha_file('hello', 'test_', '.txt')
        assert path1 == path2

    def test_different_content_different_path(self, podrun_tmp):
        path1 = _write_sha_file('hello', 'test_', '.txt')
        path2 = _write_sha_file('world', 'test_', '.txt')
        assert path1 != path2

    def test_creates_parent_dir(self, tmp_path):
        import podrun.podrun as mod

        new_tmp = str(tmp_path / 'new_subdir')
        original = mod.PODRUN_TMP
        mod.PODRUN_TMP = new_tmp
        try:
            path = _write_sha_file('test', 'test_', '.txt')
            assert os.path.exists(path)
        finally:
            mod.PODRUN_TMP = original

    def test_coexists_with_different_content(self, podrun_tmp):
        """Different content produces different files; both survive."""
        path1 = _write_sha_file('version1', 'ep_', '.sh')
        path2 = _write_sha_file('version2', 'ep_', '.sh')
        assert path1 != path2
        assert os.path.exists(path1)
        assert os.path.exists(path2)


class TestGenerateEntrypoint:
    def test_shebang(self, make_config, podrun_tmp):
        config = make_config(user_overlay=True, login=False)
        path = generate_run_entrypoint(config)
        with open(path) as f:
            first_line = f.readline()
        assert first_line.startswith('#!/bin/sh')
        assert '-l' not in first_line

    def test_login_flag(self, make_config, podrun_tmp):
        config = make_config(user_overlay=True, login=True)
        path = generate_run_entrypoint(config)
        with open(path) as f:
            first_line = f.readline()
        assert '-l' in first_line

    def test_uid_gid_in_content(self, make_config, podrun_tmp):
        config = make_config(user_overlay=True)
        path = generate_run_entrypoint(config)
        with open(path) as f:
            content = f.read()
        assert str(FAKE_UID) in content
        assert str(FAKE_GID) in content

    def test_username_in_content(self, make_config, podrun_tmp):
        config = make_config(user_overlay=True)
        path = generate_run_entrypoint(config)
        with open(path) as f:
            content = f.read()
        assert FAKE_UNAME in content

    def test_passwd_shell_patched_via_sed(self, make_config, podrun_tmp):
        """Entrypoint patches SHELL field in /etc/passwd via sed (--passwd-entry
        creates the entry with /bin/sh; entrypoint updates to resolved shell)."""
        config = make_config(user_overlay=True)
        path = generate_run_entrypoint(config)
        with open(path) as f:
            content = f.read()
        assert 'sed' in content
        assert '/bin/sh' in content
        # Should NOT create the passwd entry (--passwd-entry handles that)
        assert '>> /etc/passwd' not in content

    def test_home_export(self, make_config, podrun_tmp):
        """Entrypoint forces HOME to /home/<user> (image may override it)."""
        config = make_config(user_overlay=True)
        path = generate_run_entrypoint(config)
        with open(path) as f:
            content = f.read()
        assert f'HOME=/home/{FAKE_UNAME}' in content
        assert 'export HOME' in content

    def test_shell_detection_default(self, make_config, podrun_tmp):
        config = make_config(user_overlay=True, shell=None)
        path = generate_run_entrypoint(config)
        with open(path) as f:
            content = f.read()
        assert 'Detect shell' in content

    def test_shell_detection_custom(self, make_config, podrun_tmp):
        config = make_config(user_overlay=True, shell='zsh')
        path = generate_run_entrypoint(config)
        with open(path) as f:
            content = f.read()
        assert 'zsh' in content

    def test_caps_in_content(self, make_config, podrun_tmp):
        config = make_config(
            user_overlay=True,
            bootstrap_caps=['CAP_DAC_OVERRIDE', 'CAP_CHOWN'],
        )
        path = generate_run_entrypoint(config)
        with open(path) as f:
            content = f.read()
        assert 'dac_override' in content
        assert 'chown' in content

    def test_excluded_cap_not_in_drop_list(self, make_config, podrun_tmp):
        """When a cap is excluded from bootstrap_caps (user provided it), it must
        not appear in the setpriv or capsh drop lines."""
        config = make_config(
            user_overlay=True,
            bootstrap_caps=['CAP_CHOWN', 'CAP_FOWNER'],
        )
        path = generate_run_entrypoint(config)
        with open(path) as f:
            content = f.read()
        # CAP_DAC_OVERRIDE was excluded — must not appear in the drop block
        drop_section = content[content.index('Drop bootstrap capabilities') :]
        assert 'dac_override' not in drop_section
        # The remaining caps should still be present
        assert 'chown' in drop_section
        assert 'fowner' in drop_section

    def test_idempotent(self, make_config, podrun_tmp):
        config = make_config(user_overlay=True)
        path1 = generate_run_entrypoint(config)
        path2 = generate_run_entrypoint(config)
        assert path1 == path2

    def test_exports_in_content(self, make_config, podrun_tmp):
        import hashlib as _hl

        config = make_config(
            user_overlay=True,
            exports=['/opt/sdk/bin:./local-sdk'],
        )
        path = generate_run_entrypoint(config)
        with open(path) as f:
            content = f.read()
        assert '# Export (mount): /opt/sdk/bin' in content
        assert 'cp -a "/opt/sdk/bin/."' in content
        assert 'rm -rf "/opt/sdk/bin"' in content
        assert 'ln -sfn' in content
        expected_hash = _hl.sha256('/opt/sdk/bin'.encode()).hexdigest()[:12]
        assert f'/.podrun/exports/{expected_hash}' in content

    def test_exports_copy_only(self, make_config, podrun_tmp):
        import hashlib as _hl

        config = make_config(
            user_overlay=True,
            exports=['/opt/sdk/bin:./local-sdk:0'],
        )
        path = generate_run_entrypoint(config)
        with open(path) as f:
            content = f.read()
        assert '# Export (copy): /opt/sdk/bin' in content
        assert 'cp -a "/opt/sdk/bin/."' in content
        assert 'rm -rf' not in content
        assert 'ln -sfn' not in content
        expected_hash = _hl.sha256('/opt/sdk/bin'.encode()).hexdigest()[:12]
        assert f'/.podrun/exports/{expected_hash}' in content

    def test_exports_empty_no_export_block(self, make_config, podrun_tmp):
        config = make_config(user_overlay=True, exports=[])
        path = generate_run_entrypoint(config)
        with open(path) as f:
            content = f.read()
        assert '# Export' not in content

    def test_ready_sentinel(self, make_config, podrun_tmp):
        """Entrypoint touches READY sentinel after setup, before cap-drop."""
        config = make_config(user_overlay=True)
        path = generate_run_entrypoint(config)
        with open(path) as f:
            content = f.read()
        assert f'touch {PODRUN_READY_PATH}' in content
        # Sentinel must come before cap-drop
        ready_idx = content.index(f'touch {PODRUN_READY_PATH}')
        cap_idx = content.index('Drop bootstrap capabilities')
        assert ready_idx < cap_idx

    def test_exports_nonexistent_path_creates_symlink(self, make_config, podrun_tmp):
        """Strict export of non-existent path creates parent dirs and symlinks."""
        import hashlib as _hl

        config = make_config(
            user_overlay=True,
            exports=['/opt/nonexistent:./host-dir'],
        )
        path = generate_run_entrypoint(config)
        with open(path) as f:
            content = f.read()
        expected_hash = _hl.sha256('/opt/nonexistent'.encode()).hexdigest()[:12]
        staging = f'/.podrun/exports/{expected_hash}'
        # The fallback elif branch should create parent dirs and symlink
        assert 'mkdir -p "$(dirname "/opt/nonexistent")"' in content
        assert f'ln -sfn "{staging}" "/opt/nonexistent"' in content

    def test_exports_copy_only_no_create_for_nonexistent(self, make_config, podrun_tmp):
        """Copy-only mode does NOT have mkdir/symlink fallback for non-existent paths."""
        config = make_config(
            user_overlay=True,
            exports=['/opt/nonexistent:./host-dir:0'],
        )
        path = generate_run_entrypoint(config)
        with open(path) as f:
            content = f.read()
        assert 'mkdir -p "$(dirname "/opt/nonexistent")"' not in content

    def test_exports_sorted_order(self, make_config, podrun_tmp):
        config1 = make_config(
            user_overlay=True,
            exports=['/b:./b', '/a:./a'],
        )
        config2 = make_config(
            user_overlay=True,
            exports=['/a:./a', '/b:./b'],
        )
        path1 = generate_run_entrypoint(config1)
        path2 = generate_run_entrypoint(config2)
        assert path1 == path2  # identical SHA filename
        with open(path1) as f:
            content = f.read()
        # /a should appear before /b
        idx_a = content.index('# Export (mount): /a')
        idx_b = content.index('# Export (mount): /b')
        assert idx_a < idx_b


class TestGenerateRcSh:
    def test_returns_path(self, make_config, podrun_tmp, mock_run_os_cmd):
        mock_run_os_cmd.set_return(stdout='Intel Core i7\n')
        config = make_config(prompt_banner='test-banner')
        path = generate_rc_sh(config)
        assert os.path.exists(path)

    def test_banner_in_content(self, make_config, podrun_tmp, mock_run_os_cmd):
        mock_run_os_cmd.set_return(stdout='Intel Core i7\n')
        config = make_config(prompt_banner='my-banner')
        path = generate_rc_sh(config)
        with open(path) as f:
            content = f.read()
        assert 'my-banner' in content

    def test_podrun_cow_art(self, make_config, podrun_tmp, mock_run_os_cmd):
        mock_run_os_cmd.set_return(stdout='test\n')
        config = make_config(prompt_banner='test')
        path = generate_rc_sh(config)
        with open(path) as f:
            content = f.read()
        assert 'PODRUN' in content


class TestGenerateExecEntry:
    def test_shebang(self, make_config, podrun_tmp):
        config = make_config(user_overlay=True)
        path = generate_exec_entrypoint(config)
        with open(path) as f:
            first_line = f.readline()
        assert first_line.startswith('#!/bin/sh')

    def test_home_resolution_block(self, make_config, podrun_tmp):
        config = make_config(user_overlay=True)
        path = generate_exec_entrypoint(config)
        with open(path) as f:
            content = f.read()
        assert 'HOME resolution' in content
        assert 'export HOME' in content

    def test_shell_resolution_block(self, make_config, podrun_tmp):
        config = make_config(user_overlay=True)
        path = generate_exec_entrypoint(config)
        with open(path) as f:
            content = f.read()
        assert 'PODRUN_SHELL' in content

    def test_login_resolution_block(self, make_config, podrun_tmp):
        config = make_config(user_overlay=True)
        path = generate_exec_entrypoint(config)
        with open(path) as f:
            content = f.read()
        assert 'PODRUN_LOGIN' in content

    def test_stty_block(self, make_config, podrun_tmp):
        config = make_config(user_overlay=True)
        path = generate_exec_entrypoint(config)
        with open(path) as f:
            content = f.read()
        assert 'PODRUN_STTY_INIT' in content

    def test_ready_wait(self, make_config, podrun_tmp):
        """Exec-entrypoint waits for READY sentinel before proceeding."""
        config = make_config(user_overlay=True)
        path = generate_exec_entrypoint(config)
        with open(path) as f:
            content = f.read()
        assert PODRUN_READY_PATH in content
        assert f'while [ ! -e {PODRUN_READY_PATH} ]' in content
        # Wait must come before HOME resolution
        wait_idx = content.index(PODRUN_READY_PATH)
        home_idx = content.index('HOME resolution')
        assert wait_idx < home_idx

    def test_idempotent(self, make_config, podrun_tmp):
        config = make_config(user_overlay=True)
        path1 = generate_exec_entrypoint(config)
        path2 = generate_exec_entrypoint(config)
        assert path1 == path2

    def test_version_embedded(self, make_config, podrun_tmp):
        config = make_config(user_overlay=True)
        path = generate_exec_entrypoint(config)
        with open(path) as f:
            content = f.read()
        assert f'podrun {__version__}' in content


class TestVersionEmbedded:
    """Verify __version__ is embedded in generated scripts for traceability."""

    def test_version_embedded_in_entrypoint(self, make_config, podrun_tmp):
        config = make_config(user_overlay=True)
        path = generate_run_entrypoint(config)
        with open(path) as f:
            content = f.read()
        assert f'podrun {__version__}' in content

    def test_version_embedded_in_rc_sh(self, make_config, podrun_tmp, mock_run_os_cmd):
        mock_run_os_cmd.set_return(stdout='test\n')
        config = make_config(prompt_banner='test')
        path = generate_rc_sh(config)
        with open(path) as f:
            content = f.read()
        assert f'podrun {__version__}' in content

    def test_version_embedded_in_exec_entry(self, make_config, podrun_tmp):
        config = make_config(user_overlay=True)
        path = generate_exec_entrypoint(config)
        with open(path) as f:
            content = f.read()
        assert f'podrun {__version__}' in content
