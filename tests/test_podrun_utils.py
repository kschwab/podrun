"""Tests for Phase 2.1 — constants, utilities, and parsing helpers."""

import os
import stat
import sys

import pytest

from podrun.podrun import (
    BOOTSTRAP_CAPS,
    GID,
    PODRUN_ENTRYPOINT_PATH,
    PODRUN_EXEC_ENTRY_PATH,
    PODRUN_RC_PATH,
    PODRUN_READY_PATH,
    PODRUN_TMP,
    UID,
    UNAME,
    USER_HOME,
    _OVERLAY_FIELDS,
    _expand_export_tilde,
    _expand_volume_tilde,
    _extract_passthrough_entrypoint,
    _parse_export,
    _parse_image_ref,
    _passthrough_has_exact,
    _passthrough_has_flag,
    _passthrough_has_short_flag,
    _volume_mount_destinations,
    _write_sha_file,
    yes_no_prompt,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_uid_gid_are_ints(self):
        assert isinstance(UID, int)
        assert isinstance(GID, int)

    def test_uname_non_empty(self):
        assert isinstance(UNAME, str)
        assert len(UNAME) > 0

    def test_user_home_is_absolute(self):
        assert os.path.isabs(USER_HOME)

    def test_podrun_tmp_is_absolute(self):
        assert os.path.isabs(PODRUN_TMP)

    def test_container_paths_are_absolute(self):
        for p in (
            PODRUN_RC_PATH,
            PODRUN_ENTRYPOINT_PATH,
            PODRUN_EXEC_ENTRY_PATH,
            PODRUN_READY_PATH,
        ):
            assert p.startswith('/'), f'{p} is not absolute'

    def test_bootstrap_caps_non_empty(self):
        assert len(BOOTSTRAP_CAPS) > 0
        assert all(c.startswith('CAP_') for c in BOOTSTRAP_CAPS)

    def test_overlay_fields_use_ns_keys(self):
        """_OVERLAY_FIELDS should use run.* ns-dict keys, not bare field names."""
        for ns_key, _token in _OVERLAY_FIELDS:
            assert ns_key.startswith('run.'), f'{ns_key} missing run. prefix'


# ---------------------------------------------------------------------------
# _parse_export
# ---------------------------------------------------------------------------


class TestParseExport:
    def test_two_part_strict(self):
        assert _parse_export('/src:/dst') == ('/src', '/dst', False)

    def test_three_part_copy_only(self):
        assert _parse_export('/src:/dst:0') == ('/src', '/dst', True)

    def test_invalid_one_part(self):
        with pytest.raises(ValueError, match='expected SRC:DST'):
            _parse_export('/only-one')

    def test_invalid_three_part_not_zero(self):
        with pytest.raises(ValueError, match='expected SRC:DST'):
            _parse_export('/a:/b:1')

    def test_invalid_four_parts(self):
        with pytest.raises(ValueError, match='expected SRC:DST'):
            _parse_export('/a:/b:0:extra')


# ---------------------------------------------------------------------------
# _parse_image_ref
# ---------------------------------------------------------------------------


class TestParseImageRef:
    def test_simple_name(self):
        assert _parse_image_ref('alpine') == ('docker.io', 'alpine', 'latest')

    def test_name_with_tag(self):
        assert _parse_image_ref('alpine:3.18') == ('docker.io', 'alpine', '3.18')

    def test_registry_with_dot(self):
        reg, name, tag = _parse_image_ref('registry.io/org/app:v1')
        assert reg == 'registry.io'
        assert name == 'org/app'
        assert tag == 'v1'

    def test_registry_with_port(self):
        reg, name, tag = _parse_image_ref('localhost:5000/myimg:test')
        assert reg == 'localhost:5000'
        assert name == 'myimg'
        assert tag == 'test'

    def test_no_tag_defaults_latest(self):
        _, _, tag = _parse_image_ref('ubuntu')
        assert tag == 'latest'

    def test_org_slash_name(self):
        reg, name, tag = _parse_image_ref('library/ubuntu:22.04')
        assert reg == 'docker.io'
        assert name == 'library/ubuntu'
        assert tag == '22.04'

    def test_invalid_image(self):
        with pytest.raises(ValueError, match='Invalid image name'):
            _parse_image_ref('INVALID!!!')


# ---------------------------------------------------------------------------
# _write_sha_file
# ---------------------------------------------------------------------------


class TestWriteShaFile:
    def test_creates_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr('podrun.podrun.PODRUN_TMP', str(tmp_path))
        path = _write_sha_file('hello world', 'test_', '.sh')
        assert os.path.exists(path)
        assert path.startswith(str(tmp_path))
        assert path.endswith('.sh')
        with open(path) as f:
            assert f.read() == 'hello world'

    def test_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setattr('podrun.podrun.PODRUN_TMP', str(tmp_path))
        p1 = _write_sha_file('same content', 'pfx_', '.sh')
        p2 = _write_sha_file('same content', 'pfx_', '.sh')
        assert p1 == p2

    def test_different_content_different_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr('podrun.podrun.PODRUN_TMP', str(tmp_path))
        p1 = _write_sha_file('content A', 'pfx_', '.sh')
        p2 = _write_sha_file('content B', 'pfx_', '.sh')
        assert p1 != p2

    def test_executable_permission(self, tmp_path, monkeypatch):
        monkeypatch.setattr('podrun.podrun.PODRUN_TMP', str(tmp_path))
        path = _write_sha_file('#!/bin/sh\necho hi', 'ep_', '.sh')
        mode = os.stat(path).st_mode
        assert mode & stat.S_IXUSR

    def test_creates_parent_dirs(self, tmp_path, monkeypatch):
        nested = tmp_path / 'deep' / 'nested'
        monkeypatch.setattr('podrun.podrun.PODRUN_TMP', str(nested))
        path = _write_sha_file('x', 'p_', '.sh')
        assert os.path.exists(path)


# ---------------------------------------------------------------------------
# Passthrough flag introspection
# ---------------------------------------------------------------------------


class TestPassthroughHasFlag:
    def test_exact_match(self):
        assert _passthrough_has_flag(['--userns', 'keep-id'], '--userns')

    def test_equals_form(self):
        assert _passthrough_has_flag(['--userns=keep-id'], '--userns')

    def test_no_match(self):
        assert not _passthrough_has_flag(['--network=host'], '--userns')

    def test_empty(self):
        assert not _passthrough_has_flag([], '--userns')

    def test_partial_no_match(self):
        """--userns-foo should not match --userns (no = or exact)."""
        assert not _passthrough_has_flag(['--userns-foo'], '--userns')


class TestPassthroughHasExact:
    def test_found(self):
        assert _passthrough_has_exact(['-i', '-t', '--rm'], '--rm')

    def test_not_found(self):
        assert not _passthrough_has_exact(['-i', '-t'], '--rm')


class TestPassthroughHasShortFlag:
    def test_standalone(self):
        assert _passthrough_has_short_flag(['-i'], 'i')

    def test_combined(self):
        assert _passthrough_has_short_flag(['-it'], 't')

    def test_not_found(self):
        assert not _passthrough_has_short_flag(['-i'], 't')

    def test_ignores_long_flags(self):
        assert not _passthrough_has_short_flag(['--interactive'], 'i')

    def test_ignores_value_flag_equals_form(self):
        assert not _passthrough_has_short_flag(['-v=/path/containing/it'], 'i')
        assert not _passthrough_has_short_flag(['-v=/path/containing/it'], 't')

    def test_ignores_short_value_flag_with_equals(self):
        assert not _passthrough_has_short_flag(['-l=value'], 'l')


class TestExtractPassthroughEntrypoint:
    def test_equals_form(self):
        ep, filtered = _extract_passthrough_entrypoint(['--entrypoint=/bin/sh', '-it'])
        assert ep == '/bin/sh'
        assert filtered == ['-it']

    def test_space_form(self):
        ep, filtered = _extract_passthrough_entrypoint(['--entrypoint', '/bin/sh', '-it'])
        assert ep == '/bin/sh'
        assert filtered == ['-it']

    def test_no_entrypoint(self):
        ep, filtered = _extract_passthrough_entrypoint(['-it', '--rm'])
        assert ep is None
        assert filtered == ['-it', '--rm']

    def test_multiple_takes_last(self):
        ep, filtered = _extract_passthrough_entrypoint(
            ['--entrypoint=first', '--entrypoint=second']
        )
        assert ep == 'second'
        assert filtered == []

    def test_empty(self):
        ep, filtered = _extract_passthrough_entrypoint([])
        assert ep is None
        assert filtered == []


class TestVolumeMountDestinations:
    def test_extracts_dests(self):
        dests = _volume_mount_destinations(['-v=/host:/container', '-v=/a:/b:ro'])
        assert '/container' in dests
        assert '/b' in dests

    def test_long_form(self):
        dests = _volume_mount_destinations(['--volume=/x:/y'])
        assert '/y' in dests

    def test_ignores_non_volume(self):
        dests = _volume_mount_destinations(['-e=FOO=bar', '--rm'])
        assert len(dests) == 0

    def test_multiple_arg_lists(self):
        dests = _volume_mount_destinations(['-v=/a:/b'], ['-v=/c:/d'])
        assert dests == {'/b', '/d'}

    def test_tilde_dest_expanded(self):
        dests = _volume_mount_destinations(['-v=/host:~/subdir'])
        assert f'/home/{UNAME}/subdir' in dests

    def test_empty(self):
        assert _volume_mount_destinations([]) == set()

    def test_mount_equals_form(self):
        dests = _volume_mount_destinations(['--mount=source=/host,target=/ctr,type=bind'])
        assert '/ctr' in dests

    def test_mount_space_form(self):
        dests = _volume_mount_destinations(['--mount', 'source=/host,target=/ctr,type=bind'])
        assert '/ctr' in dests

    def test_mount_dst_alias(self):
        dests = _volume_mount_destinations(['--mount=type=volume,src=myvol,dst=/data'])
        assert '/data' in dests

    def test_mount_destination_alias(self):
        dests = _volume_mount_destinations(['--mount=type=bind,source=/a,destination=/b'])
        assert '/b' in dests

    def test_mount_and_volume_combined(self):
        dests = _volume_mount_destinations(['-v=/a:/b', '--mount=source=/c,target=/d,type=bind'])
        assert dests == {'/b', '/d'}


# ---------------------------------------------------------------------------
# Tilde expansion
# ---------------------------------------------------------------------------


class TestExpandVolumeTilde:
    def test_source_tilde(self):
        result = _expand_volume_tilde(['-v=~/src:/dst'])
        assert result == [f'-v={USER_HOME}/src:/dst']

    def test_dest_tilde(self):
        result = _expand_volume_tilde(['-v=/src:~/dst'])
        assert result == [f'-v=/src:/home/{UNAME}/dst']

    def test_both_tildes(self):
        result = _expand_volume_tilde(['-v=~/src:~/dst'])
        assert result == [f'-v={USER_HOME}/src:/home/{UNAME}/dst']

    def test_long_form(self):
        result = _expand_volume_tilde(['--volume=~/src:/dst'])
        assert result == [f'--volume={USER_HOME}/src:/dst']

    def test_with_options(self):
        result = _expand_volume_tilde(['-v=~/src:~/dst:ro'])
        assert result == [f'-v={USER_HOME}/src:/home/{UNAME}/dst:ro']

    def test_non_volume_unchanged(self):
        result = _expand_volume_tilde(['-e=FOO=bar', '--rm'])
        assert result == ['-e=FOO=bar', '--rm']

    def test_no_tilde_unchanged(self):
        result = _expand_volume_tilde(['-v=/a:/b'])
        assert result == ['-v=/a:/b']

    def test_single_part(self):
        result = _expand_volume_tilde(['-v=~/only'])
        assert result == [f'-v={USER_HOME}/only']

    def test_space_form_single_part_tilde(self):
        """Space-form volume with only one colon-part expands source tilde."""
        result = _expand_volume_tilde(['-v', '~/only'])
        assert result == ['-v', f'{USER_HOME}/only']


class TestExpandExportTilde:
    def test_container_tilde(self):
        result = _expand_export_tilde(['~/src:/dst'])
        assert result == [f'/home/{UNAME}/src:/dst']

    def test_host_tilde(self):
        result = _expand_export_tilde(['/src:~/dst'])
        assert result == [f'/src:{USER_HOME}/dst']

    def test_both_tildes(self):
        result = _expand_export_tilde(['~/src:~/dst'])
        assert result == [f'/home/{UNAME}/src:{USER_HOME}/dst']

    def test_copy_only_flag_preserved(self):
        result = _expand_export_tilde(['~/src:~/dst:0'])
        assert result == [f'/home/{UNAME}/src:{USER_HOME}/dst:0']

    def test_no_tilde_unchanged(self):
        result = _expand_export_tilde(['/a:/b'])
        assert result == ['/a:/b']

    def test_single_part(self):
        result = _expand_export_tilde(['~/only'])
        assert result == [f'/home/{UNAME}/only']


# ---------------------------------------------------------------------------
# yes_no_prompt
# ---------------------------------------------------------------------------


class TestYesNoPrompt:
    def test_non_interactive_default_yes(self, capsys):
        result = yes_no_prompt('Continue?', answer_default=True, is_interactive=False)
        assert result is True
        assert 'Y/n' in capsys.readouterr().err

    def test_non_interactive_default_no(self, capsys):
        result = yes_no_prompt('Continue?', answer_default=False, is_interactive=False)
        assert result is False
        assert 'N/y' in capsys.readouterr().err

    def test_interactive_yes(self, monkeypatch, capsys):
        monkeypatch.setattr('builtins.input', lambda: 'y')
        result = yes_no_prompt('Continue?', answer_default=False, is_interactive=True)
        assert result is True

    def test_interactive_no(self, monkeypatch, capsys):
        monkeypatch.setattr('builtins.input', lambda: 'n')
        result = yes_no_prompt('Continue?', answer_default=True, is_interactive=True)
        assert result is False

    def test_interactive_empty_uses_default_yes(self, monkeypatch, capsys):
        monkeypatch.setattr('builtins.input', lambda: '')
        result = yes_no_prompt('Continue?', answer_default=True, is_interactive=True)
        assert result is True

    def test_interactive_empty_uses_default_no(self, monkeypatch, capsys):
        monkeypatch.setattr('builtins.input', lambda: '')
        result = yes_no_prompt('Continue?', answer_default=False, is_interactive=False)
        assert result is False

    def test_interactive_retry_on_invalid_then_yes(self, monkeypatch, capsys):
        answers = iter(['maybe', 'y'])
        monkeypatch.setattr('builtins.input', lambda: next(answers))
        result = yes_no_prompt('Continue?', answer_default=False, is_interactive=True)
        assert result is True
        assert 'Please answer yes or no' in capsys.readouterr().err

    def test_interactive_retry_on_invalid_then_no(self, monkeypatch, capsys):
        answers = iter(['x', 'n'])
        monkeypatch.setattr('builtins.input', lambda: next(answers))
        result = yes_no_prompt('Continue?', answer_default=True, is_interactive=True)
        assert result is False


# ---------------------------------------------------------------------------
# _resolve_script_command
# ---------------------------------------------------------------------------


class TestResolveScriptCommand:
    """Tests for Python config script command building."""

    def test_uses_sys_executable(self):
        from podrun.podrun import _resolve_script_command

        cmd = _resolve_script_command('/some/script.py')
        assert sys.executable in cmd
        assert '/some/script.py' in cmd

    def test_quoting_with_spaces(self):
        from podrun.podrun import _resolve_script_command

        cmd = _resolve_script_command('/path with spaces/script.py')
        assert sys.executable in cmd
        assert 'path with spaces' in cmd
