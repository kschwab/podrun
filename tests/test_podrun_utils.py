"""Tests for Phase 2.1 — constants, utilities, and parsing helpers."""

import os
import stat

import pytest

import podrun.podrun as podrun_mod
from podrun.podrun import (
    BOOTSTRAP_CAPS,
    ENV_PODRUN_CONTAINER,
    ENV_PODRUN_HOST_TMP,
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
    PODRUN_HOST_TMP_MOUNT,
    _daemon_dir,
    _expand_export_tilde,
    _extract_passthrough_entrypoint,
    _parse_export,
    _parse_image_ref,
    _passthrough_has_exact,
    _passthrough_has_flag,
    _passthrough_has_short_flag,
    _process_volume_args,
    _read_mount_manifest,
    _staging_dir,
    _volume_mount_destinations,
    _write_mount_manifest,
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

    def test_nested_remote_writes_staging_returns_daemon(self, tmp_path, monkeypatch):
        """In nested-remote mode, file is written to _staging_dir but path uses _daemon_dir."""
        host_tmp_mount = tmp_path / 'host-tmp'
        host_tmp_mount.mkdir()
        monkeypatch.setattr(podrun_mod, 'PODRUN_HOST_TMP_MOUNT', str(host_tmp_mount))
        monkeypatch.setenv(ENV_PODRUN_HOST_TMP, '/real/host/podrun-tmp')
        monkeypatch.setenv(ENV_PODRUN_CONTAINER, '1')
        path = _write_sha_file('nested content', 'ep_', '.sh')
        # Returned path references the daemon-visible directory
        assert path.startswith('/real/host/podrun-tmp/')
        # But the file was physically written to the staging dir
        filename = os.path.basename(path)
        written = os.path.join(str(host_tmp_mount), filename)
        assert os.path.exists(written)
        with open(written) as f:
            assert f.read() == 'nested content'


# ---------------------------------------------------------------------------
# _staging_dir / _daemon_dir
# ---------------------------------------------------------------------------


class TestStagingDir:
    def test_normal_returns_podrun_tmp(self, tmp_path, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path))
        assert _staging_dir() == str(tmp_path)

    def test_nested_remote_returns_host_tmp_mount(self, monkeypatch):
        monkeypatch.setenv(ENV_PODRUN_HOST_TMP, '/host/podrun-tmp')
        monkeypatch.setenv(ENV_PODRUN_CONTAINER, '1')
        assert _staging_dir() == PODRUN_HOST_TMP_MOUNT

    def test_host_tmp_without_container_returns_podrun_tmp(self, tmp_path, monkeypatch):
        """PODRUN_HOST_TMP alone (without PODRUN_CONTAINER) is not nested-remote."""
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path))
        monkeypatch.setenv(ENV_PODRUN_HOST_TMP, '/host/podrun-tmp')
        assert _staging_dir() == str(tmp_path)

    def test_container_without_host_tmp_returns_podrun_tmp(self, tmp_path, monkeypatch):
        """PODRUN_CONTAINER alone (without PODRUN_HOST_TMP) is not nested-remote."""
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path))
        monkeypatch.setenv(ENV_PODRUN_CONTAINER, '1')
        assert _staging_dir() == str(tmp_path)


class TestDaemonDir:
    def test_normal_returns_podrun_tmp(self, tmp_path, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path))
        assert _daemon_dir() == str(tmp_path)

    def test_nested_remote_returns_host_tmp_value(self, monkeypatch):
        monkeypatch.setenv(ENV_PODRUN_HOST_TMP, '/host/podrun-tmp')
        monkeypatch.setenv(ENV_PODRUN_CONTAINER, '1')
        assert _daemon_dir() == '/host/podrun-tmp'

    def test_host_tmp_without_container_returns_podrun_tmp(self, tmp_path, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path))
        monkeypatch.setenv(ENV_PODRUN_HOST_TMP, '/host/podrun-tmp')
        assert _daemon_dir() == str(tmp_path)

    def test_container_without_host_tmp_returns_podrun_tmp(self, tmp_path, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path))
        monkeypatch.setenv(ENV_PODRUN_CONTAINER, '1')
        assert _daemon_dir() == str(tmp_path)


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
# _write_mount_manifest / _read_mount_manifest
# ---------------------------------------------------------------------------


class TestMountManifest:
    def test_write_and_read_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path))
        mount_map = {'/.podrun/run-entrypoint.sh': '/host/ep.sh', '/home/user/.ssh': '/host/.ssh'}
        _write_mount_manifest(mount_map)
        m = _read_mount_manifest()
        assert m['mounts']['/.podrun/run-entrypoint.sh'] == '/host/ep.sh'
        assert m['mounts']['/home/user/.ssh'] == '/host/.ssh'

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path))
        m = _read_mount_manifest()
        assert m == {'mounts': {}, 'copy_staging': {}}

    def test_copy_staging_section(self, tmp_path, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path))
        cs = [
            ('/home/user/.ssh', '/home/user/.ssh'),
            ('/home/user/.gitconfig', '/home/user/.gitconfig'),
        ]
        _write_mount_manifest({'/ctr/.ssh': '/host/.ssh'}, cs)
        m = _read_mount_manifest()
        assert m['copy_staging']['/home/user/.ssh'] == '/home/user/.ssh'
        assert m['copy_staging']['/home/user/.gitconfig'] == '/home/user/.gitconfig'

    def test_empty_copy_staging(self, tmp_path, monkeypatch):
        monkeypatch.setattr(podrun_mod, 'PODRUN_TMP', str(tmp_path))
        _write_mount_manifest({'/b': '/a'})
        m = _read_mount_manifest()
        assert m['copy_staging'] == {}


# ---------------------------------------------------------------------------
# _process_volume_args
# ---------------------------------------------------------------------------


class TestProcessVolumeArgs:
    def test_tilde_expansion(self):
        args = ['-v=~/src:/dst:ro']
        result, cs, mm = _process_volume_args(args, expand_tilde=True)
        assert result[0] == f'-v={podrun_mod.USER_HOME}/src:/dst:ro'

    def test_tilde_expansion_bare(self):
        args = ['-v=~:/dst']
        result, cs, mm = _process_volume_args(args, expand_tilde=True)
        assert result[0] == f'-v={podrun_mod.USER_HOME}:/dst'

    def test_copy_staging_extraction(self):
        args = ['-v=/a:/b:0', '-v=/c:/d:ro']
        result, cs, mm = _process_volume_args(args)
        assert len(result) == 1
        assert result[0] == '-v=/c:/d:ro'
        assert cs == [('/a', '/b')]

    def test_copy_staging_extraction_space_form(self):
        args = ['-v', '/a:/b:0', '-v=/c:/d:ro']
        result, cs, mm = _process_volume_args(args)
        assert len(result) == 1
        assert result[0] == '-v=/c:/d:ro'
        assert cs == [('/a', '/b')]

    def test_manifest_translation_equals(self):
        mounts = {'/app': '/real/host/project'}
        args = ['-v=/app:/app:z', '--env=FOO=bar']
        result, _, mm = _process_volume_args(args, manifest_mounts=mounts)
        assert result == ['-v=/real/host/project:/app:z', '--env=FOO=bar']

    def test_manifest_translation_space(self):
        mounts = {'/home/user/.vimrc': '/real/home/.vimrc'}
        args = ['-v', '/home/user/.vimrc:/home/user/.vimrc:ro,z']
        result, _, _ = _process_volume_args(args, manifest_mounts=mounts)
        assert result == ['-v', '/real/home/.vimrc:/home/user/.vimrc:ro,z']

    def test_manifest_translation_mount_spec(self):
        mounts = {'/app': '/real/project'}
        args = ['--mount=type=bind,source=/app,target=/app']
        result, _, _ = _process_volume_args(args, manifest_mounts=mounts)
        assert result == ['--mount=type=bind,source=/real/project,target=/app']

    def test_manifest_translation_mount_space(self):
        mounts = {'/app': '/real/project'}
        args = ['--mount', 'type=bind,src=/app,dst=/app']
        result, _, _ = _process_volume_args(args, manifest_mounts=mounts)
        assert result == ['--mount', 'type=bind,src=/real/project,dst=/app']

    def test_mount_map_built(self):
        args = ['-v=/a:/b:ro', '-v', '/c:/d', '--mount=type=bind,source=/e,target=/f']
        _, _, mm = _process_volume_args(args)
        assert mm == {'/b': '/a', '/d': '/c', '/f': '/e'}

    def test_no_match_unchanged(self):
        mounts = {'/ctr': '/host'}
        args = ['-v=/etc/localtime:/etc/localtime:ro']
        result, _, _ = _process_volume_args(args, manifest_mounts=mounts)
        assert result == ['-v=/etc/localtime:/etc/localtime:ro']

    def test_non_volume_args_passthrough(self):
        args = ['--rm', '-it', '--env=FOO=bar']
        result, cs, mm = _process_volume_args(args)
        assert result == args
        assert cs == []
        assert mm == {}

    def test_all_features_combined(self):
        """Tilde expansion + :0 extraction + manifest translation in one pass."""
        mounts = {'/app': '/real/project'}
        args = ['-v=~/src:~/dst:ro', '-v=~/.ssh:~/.ssh:0', '-v=/app:/app:z']
        result, cs, mm = _process_volume_args(
            args,
            expand_tilde=True,
            manifest_mounts=mounts,
        )
        # Tilde expanded, :0 extracted, /app translated
        assert len(result) == 2
        assert '~' not in result[0]
        assert result[1] == '-v=/real/project:/app:z'
        assert len(cs) == 1  # .ssh extracted
        assert mm['/app'] == '/real/project'

    def test_daemon_visible_sources_untouched(self):
        """Sources already daemon-visible (not in manifest dests) stay unchanged."""
        mounts = {'/.podrun/run-entrypoint.sh': '/host/podrun-tmp/ep_old.sh'}
        args = ['-v=/host/podrun-tmp/ep_new.sh:/.podrun/run-entrypoint.sh:ro,z']
        result, _, _ = _process_volume_args(args, manifest_mounts=mounts)
        # Source is NOT a manifest destination, so untouched
        assert result == args
