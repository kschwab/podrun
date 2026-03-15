import json
import subprocess

import pytest

from podrun.podrun import (
    _devcontainer_project_dir,
    _devcontainer_to_ns,
    _expand_devcontainer_vars,
    _strip_jsonc,
    build_run_command,
    devcontainer_run_args,
    extract_podrun_config,
    find_devcontainer_json,
    parse_args,
    parse_config_tokens,
    parse_devcontainer_json,
    resolve_config,
    run_config_scripts,
)

import podrun.podrun as podrun_mod

pytestmark = pytest.mark.usefixtures('podman_binary')


@pytest.fixture
def tmp_project(tmp_path):
    """Create a temporary project directory for devcontainer tests."""
    return tmp_path


# ---------------------------------------------------------------------------
# TestRunConfigScripts
# ---------------------------------------------------------------------------


class TestRunConfigScripts:
    def test_single_script(self, mock_run_os_cmd):
        mock_run_os_cmd.set_return(stdout='--rm --name test')
        tokens = run_config_scripts(['/path/to/script.sh'])
        assert tokens == ['--rm', '--name', 'test']
        assert len(mock_run_os_cmd.calls) == 1

    def test_multiple_scripts_concatenated(self, mock_run_os_cmd):
        mock_run_os_cmd.set_side_effect(
            [
                subprocess.CompletedProcess(args='', returncode=0, stdout='--rm'),
                subprocess.CompletedProcess(args='', returncode=0, stdout='--name test'),
            ]
        )
        tokens = run_config_scripts(['/a.sh', '/b.sh'])
        assert tokens == ['--rm', '--name', 'test']
        assert len(mock_run_os_cmd.calls) == 2

    def test_empty_output(self, mock_run_os_cmd):
        mock_run_os_cmd.set_return(stdout='')
        tokens = run_config_scripts(['/empty.sh'])
        assert tokens == []

    def test_failure_exits(self, mock_run_os_cmd):
        mock_run_os_cmd.set_return(returncode=1, stderr='boom')
        with pytest.raises(SystemExit) as exc_info:
            run_config_scripts(['/bad.sh'])
        assert exc_info.value.code == 1

    def test_empty_list(self, mock_run_os_cmd):
        tokens = run_config_scripts([])
        assert tokens == []
        assert len(mock_run_os_cmd.calls) == 0


# ---------------------------------------------------------------------------
# TestParseConfigTokens
# ---------------------------------------------------------------------------


class TestParseConfigTokens:
    def test_root_flags(self):
        ns, pt = parse_config_tokens(['--local-store', '/s', '--local-store-auto-init'])
        assert ns['root.local_store'] == '/s'
        assert ns['root.local_store_auto_init'] is True
        assert pt == []

    def test_run_flags(self):
        ns, pt = parse_config_tokens(['--name', 'test', '--session'])
        assert ns['run.name'] == 'test'
        assert ns['run.session'] is True
        assert pt == []

    def test_mixed_root_and_run(self):
        ns, pt = parse_config_tokens(['--local-store', '/s', '--name', 'test'])
        assert ns['root.local_store'] == '/s'
        assert ns['run.name'] == 'test'

    def test_passthrough(self):
        ns, pt = parse_config_tokens(['--rm', '-e', 'FOO=bar'])
        assert '--rm' in pt or '-e' in pt  # these are podman flags
        # Check that podman flags pass through
        assert '-e' in pt
        assert 'FOO=bar' in pt

    def test_empty_tokens(self):
        ns, pt = parse_config_tokens([])
        assert ns == {}
        assert pt == []

    def test_non_none_only(self):
        """Only non-None values should be present in the result dict."""
        ns, pt = parse_config_tokens(['--name', 'test'])
        assert 'run.name' in ns
        # None values from the default namespace should not be present
        assert ns.get('run.session') is None or 'run.session' not in ns

    def test_rejects_config(self):
        with pytest.raises(SystemExit):
            parse_config_tokens(['--devconfig', '/path/to/dc.json', '--name', 'test'])

    def test_rejects_config_script(self):
        with pytest.raises(SystemExit):
            parse_config_tokens(['--config-script', '/other.sh', '--name', 'test'])

    def test_rejects_no_devconfig(self):
        with pytest.raises(SystemExit):
            parse_config_tokens(['--no-devconfig', '--name', 'test'])


# ---------------------------------------------------------------------------
# TestStripJsonc
# ---------------------------------------------------------------------------


class TestStripJsonc:
    def test_line_comments(self):
        text = '{\n  // comment\n  "key": "value"\n}'
        result = json.loads(_strip_jsonc(text))
        assert result == {'key': 'value'}

    def test_block_comments(self):
        text = '{\n  /* block */\n  "key": "value"\n}'
        result = json.loads(_strip_jsonc(text))
        assert result == {'key': 'value'}

    def test_trailing_commas(self):
        text = '{"a": 1, "b": 2,}'
        result = json.loads(_strip_jsonc(text))
        assert result == {'a': 1, 'b': 2}

    def test_trailing_comma_in_array(self):
        text = '{"items": [1, 2, 3,]}'
        result = json.loads(_strip_jsonc(text))
        assert result == {'items': [1, 2, 3]}

    def test_strings_preserved(self):
        text = '{"url": "http://example.com/path"}'
        result = json.loads(_strip_jsonc(text))
        assert result == {'url': 'http://example.com/path'}

    def test_string_with_comment_like_content(self):
        text = '{"val": "has // slashes and /* stars */"}'
        result = json.loads(_strip_jsonc(text))
        assert result['val'] == 'has // slashes and /* stars */'

    def test_plain_json_unchanged(self):
        text = '{"key": "value", "num": 42}'
        result = json.loads(_strip_jsonc(text))
        assert result == {'key': 'value', 'num': 42}

    def test_escaped_quotes_in_strings(self):
        """Backslash-escaped quotes inside strings are preserved (lines 1317-1318)."""
        text = r'{"path": "C:\\Users\\me", "msg": "say \"hello\""}'
        result = json.loads(_strip_jsonc(text))
        assert result['path'] == 'C:\\Users\\me'
        assert result['msg'] == 'say "hello"'

    def test_escaped_quotes_with_comment_after(self):
        text = r'{"val": "has \\ backslash"} // comment'
        result = json.loads(_strip_jsonc(text))
        assert result == {'val': 'has \\ backslash'}


# ---------------------------------------------------------------------------
# TestParseDevcontainerJson — error paths
# ---------------------------------------------------------------------------


class TestParseDevcontainerJsonErrors:
    def test_dir_without_devcontainer_json(self, tmp_path):
        """Directory with no devcontainer.json anywhere exits with error."""
        empty_dir = tmp_path / 'empty'
        empty_dir.mkdir()
        with pytest.raises(SystemExit):
            parse_devcontainer_json(str(empty_dir))


# ---------------------------------------------------------------------------
# TestFindDevcontainerJson
# ---------------------------------------------------------------------------


class TestFindDevcontainerJson:
    def test_standard_location(self, tmp_project):
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text('{}')
        result = find_devcontainer_json(str(tmp_project))
        assert result == dc_file

    def test_shorthand_location(self, tmp_project):
        dc_file = tmp_project / '.devcontainer.json'
        dc_file.write_text('{}')
        result = find_devcontainer_json(str(tmp_project))
        assert result == dc_file

    def test_named_config(self, tmp_project):
        dc_dir = tmp_project / '.devcontainer' / 'myconfig'
        dc_dir.mkdir(parents=True)
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text('{}')
        result = find_devcontainer_json(str(tmp_project))
        assert result == dc_file

    def test_parent_walk(self, tmp_project):
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text('{}')
        child = tmp_project / 'sub' / 'deep'
        child.mkdir(parents=True)
        result = find_devcontainer_json(str(child))
        assert result == dc_file

    def test_not_found(self, tmp_project):
        result = find_devcontainer_json(str(tmp_project))
        assert result is None

    def test_standard_takes_priority_over_shorthand(self, tmp_project):
        # Standard location
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        standard = dc_dir / 'devcontainer.json'
        standard.write_text('{"source": "standard"}')
        # Shorthand
        shorthand = tmp_project / '.devcontainer.json'
        shorthand.write_text('{"source": "shorthand"}')
        result = find_devcontainer_json(str(tmp_project))
        assert result == standard


# ---------------------------------------------------------------------------
# TestParseDevcontainerJson
# ---------------------------------------------------------------------------


class TestParseDevcontainerJson:
    def test_none_returns_empty(self):
        assert parse_devcontainer_json(None) == {}

    def test_valid_file(self, tmp_project):
        f = tmp_project / 'devcontainer.json'
        f.write_text('{"image": "alpine"}')
        result = parse_devcontainer_json(str(f))
        assert result == {'image': 'alpine'}

    def test_jsonc_file(self, tmp_project):
        f = tmp_project / 'devcontainer.json'
        f.write_text('{\n  // comment\n  "image": "alpine",\n}')
        result = parse_devcontainer_json(str(f))
        assert result == {'image': 'alpine'}

    def test_directory_path(self, tmp_project):
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        f = dc_dir / 'devcontainer.json'
        f.write_text('{"image": "ubuntu"}')
        result = parse_devcontainer_json(str(dc_dir))
        assert result == {'image': 'ubuntu'}


# ---------------------------------------------------------------------------
# TestExtractPodrunConfig
# ---------------------------------------------------------------------------


class TestExtractPodrunConfig:
    def test_present(self):
        dc = {'customizations': {'podrun': {'adhoc': True}}}
        assert extract_podrun_config(dc) == {'adhoc': True}

    def test_absent(self):
        assert extract_podrun_config({}) == {}

    def test_no_customizations(self):
        assert extract_podrun_config({'image': 'alpine'}) == {}

    def test_no_podrun_in_customizations(self):
        dc = {'customizations': {'vscode': {}}}
        assert extract_podrun_config(dc) == {}


# ---------------------------------------------------------------------------
# TestDevcontainerRunArgs
# ---------------------------------------------------------------------------


class TestDevcontainerRunArgs:
    def test_mounts_string(self):
        dc = {'mounts': ['type=bind,src=/a,dst=/b']}
        args = devcontainer_run_args(dc, {})
        assert '--mount=type=bind,src=/a,dst=/b' in args

    def test_mounts_dict(self):
        dc = {'mounts': [{'type': 'bind', 'src': '/a', 'dst': '/b'}]}
        args = devcontainer_run_args(dc, {})
        assert '--mount=type=bind,src=/a,dst=/b' in args

    def test_cap_add(self):
        dc = {'capAdd': ['SYS_PTRACE', 'NET_ADMIN']}
        args = devcontainer_run_args(dc, {})
        assert '--cap-add=SYS_PTRACE' in args
        assert '--cap-add=NET_ADMIN' in args

    def test_security_opt(self):
        dc = {'securityOpt': ['seccomp=unconfined']}
        args = devcontainer_run_args(dc, {})
        assert '--security-opt=seccomp=unconfined' in args

    def test_privileged(self):
        dc = {'privileged': True}
        args = devcontainer_run_args(dc, {})
        assert '--privileged' in args

    def test_privileged_false(self):
        dc = {'privileged': False}
        args = devcontainer_run_args(dc, {})
        assert '--privileged' not in args

    def test_init(self):
        dc = {'init': True}
        args = devcontainer_run_args(dc, {})
        assert '--init' in args

    def test_run_args(self):
        dc = {'runArgs': ['--rm', '--network=host']}
        args = devcontainer_run_args(dc, {})
        assert '--rm' in args
        assert '--network=host' in args

    def test_empty(self):
        assert devcontainer_run_args({}, {}) == []

    def test_combined(self):
        dc = {
            'mounts': ['type=bind,src=/a,dst=/b'],
            'capAdd': ['SYS_PTRACE'],
            'privileged': True,
            'init': True,
            'runArgs': ['--rm'],
        }
        args = devcontainer_run_args(dc, {})
        assert len(args) == 5
        assert '--mount=type=bind,src=/a,dst=/b' in args
        assert '--cap-add=SYS_PTRACE' in args
        assert '--privileged' in args
        assert '--init' in args
        assert '--rm' in args

    def test_dc_from_cli_returns_empty(self):
        """When devcontainer CLI is driving, no args are emitted."""
        dc = {
            'mounts': ['type=bind,src=/a,dst=/b'],
            'capAdd': ['SYS_PTRACE'],
            'workspaceMount': 'source=/host,target=/app,type=bind',
            'workspaceFolder': '/app',
        }
        ns = {'internal.dc_from_cli': True}
        assert devcontainer_run_args(dc, ns) == []


# ---------------------------------------------------------------------------
# TestDevcontainerToNs
# ---------------------------------------------------------------------------


class TestDevcontainerToNs:
    def test_root_keys(self):
        cfg = {'localStore': '/s', 'localStoreAutoInit': True}
        ns = _devcontainer_to_ns(cfg)
        assert ns['root.local_store'] == '/s'
        assert ns['root.local_store_auto_init'] is True

    def test_root_store_ignore_key(self):
        cfg = {'localStoreIgnore': True}
        ns = _devcontainer_to_ns(cfg)
        assert ns['root.local_store_ignore'] is True

    def test_root_storage_driver_key(self):
        cfg = {'storageDriver': 'vfs'}
        ns = _devcontainer_to_ns(cfg)
        assert ns['root.storage_driver'] == 'vfs'

    def test_all_store_keys(self):
        cfg = {
            'localStore': '/s',
            'localStoreAutoInit': True,
            'localStoreIgnore': False,
            'storageDriver': 'overlay',
        }
        ns = _devcontainer_to_ns(cfg)
        assert ns['root.local_store'] == '/s'
        assert ns['root.local_store_auto_init'] is True
        assert ns['root.local_store_ignore'] is False
        assert ns['root.storage_driver'] == 'overlay'

    def test_run_keys(self):
        cfg = {'name': 'test', 'adhoc': True, 'shell': '/bin/zsh'}
        ns = _devcontainer_to_ns(cfg)
        assert ns['run.name'] == 'test'
        assert ns['run.adhoc'] is True
        assert ns['run.shell'] == '/bin/zsh'

    def test_non_none_only(self):
        cfg = {'name': 'test'}
        ns = _devcontainer_to_ns(cfg)
        assert 'run.name' in ns
        assert 'run.adhoc' not in ns

    def test_empty(self):
        assert _devcontainer_to_ns({}) == {}


# ---------------------------------------------------------------------------
# TestResolveConfig
# ---------------------------------------------------------------------------


class TestResolveConfig:
    def _resolve(self, argv, monkeypatch, dc=None, dc_json_path=None, script_stdout=None):
        """Helper to parse + resolve with controlled config sources."""
        monkeypatch.setattr(
            podrun_mod,
            'find_devcontainer_json',
            lambda start_dir=None: dc_json_path,
        )

        if script_stdout is not None:
            monkeypatch.setattr(
                podrun_mod,
                'run_os_cmd',
                lambda cmd: subprocess.CompletedProcess(
                    args='', returncode=0, stdout=script_stdout
                ),
            )

        if dc is not None:
            monkeypatch.setattr(podrun_mod, 'parse_devcontainer_json', lambda path: dc)

        result = parse_args(argv)
        return resolve_config(result)

    def test_cli_only(self, monkeypatch):
        r = self._resolve(
            ['--no-devconfig', 'run', '--name', 'test', 'alpine'],
            monkeypatch,
        )
        assert r.ns['run.name'] == 'test'
        assert 'alpine' in r.trailing_args

    def test_dc_only(self, monkeypatch, tmp_project):
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text(
            json.dumps(
                {
                    'image': 'ubuntu:22.04',
                    'customizations': {'podrun': {'adhoc': True, 'shell': '/bin/zsh'}},
                }
            )
        )
        r = self._resolve(['run'], monkeypatch, dc_json_path=dc_file)
        assert r.ns.get('run.adhoc') is True
        assert r.ns.get('run.shell') == '/bin/zsh'
        # Image from devcontainer
        assert 'ubuntu:22.04' in r.trailing_args

    def test_cli_overrides_script_overrides_dc(self, monkeypatch, tmp_project):
        """CLI > config-script > devcontainer.json precedence."""
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text(
            json.dumps(
                {
                    'image': 'dc-image',
                    'customizations': {
                        'podrun': {
                            'name': 'dc-name',
                            'shell': '/bin/dc-shell',
                            'session': True,
                        }
                    },
                }
            )
        )
        # Script sets name and shell
        r = self._resolve(
            ['--config-script', '/s.sh', 'run', '--name', 'cli-name', 'alpine'],
            monkeypatch,
            dc_json_path=dc_file,
            script_stdout='--name script-name --shell /bin/script-shell',
        )
        # CLI wins for name
        assert r.ns['run.name'] == 'cli-name'
        # Script wins for shell (CLI didn't set it)
        assert r.ns['run.shell'] == '/bin/script-shell'
        # DC wins for session (neither CLI nor script set it)
        assert r.ns.get('run.session') is True

    def test_multiple_scripts(self, monkeypatch):
        """Multiple --config-script: tokens concatenated."""
        call_count = [0]

        def fake_run_os_cmd(cmd):
            call_count[0] += 1
            if call_count[0] == 1:
                return subprocess.CompletedProcess(args='', returncode=0, stdout='--rm')
            return subprocess.CompletedProcess(args='', returncode=0, stdout='--name from-script')

        monkeypatch.setattr(podrun_mod, 'run_os_cmd', fake_run_os_cmd)
        monkeypatch.setattr(podrun_mod, 'find_devcontainer_json', lambda start_dir=None: None)

        r = parse_args(
            [
                '--config-script',
                '/a.sh',
                '--config-script',
                '/b.sh',
                '--no-devconfig',
                'run',
                'alpine',
            ]
        )
        r = resolve_config(r)
        # --name from script should be set
        assert r.ns['run.name'] == 'from-script'

    def test_dc_config_script_single(self, monkeypatch, tmp_project):
        """configScript from devcontainer.json runs when no CLI --config-script."""
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text(
            json.dumps(
                {
                    'image': 'alpine',
                    'customizations': {'podrun': {'configScript': '/dc-script.sh'}},
                }
            )
        )
        r = self._resolve(
            ['run'],
            monkeypatch,
            dc_json_path=dc_file,
            script_stdout='--session',
        )
        assert r.ns.get('run.session') is True

    def test_dc_config_script_list(self, monkeypatch, tmp_project):
        """configScript as a list in devcontainer.json — all scripts run in order."""
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text(
            json.dumps(
                {
                    'image': 'alpine',
                    'customizations': {
                        'podrun': {
                            'configScript': ['/dc-a.sh', '/dc-b.sh'],
                        }
                    },
                }
            )
        )
        calls = []

        def fake_run_os_cmd(cmd):
            calls.append(cmd)
            if len(calls) == 1:
                return subprocess.CompletedProcess(args='', returncode=0, stdout='--session')
            return subprocess.CompletedProcess(args='', returncode=0, stdout='--shell /bin/zsh')

        monkeypatch.setattr(podrun_mod, 'run_os_cmd', fake_run_os_cmd)
        monkeypatch.setattr(podrun_mod, 'find_devcontainer_json', lambda start_dir=None: dc_file)
        monkeypatch.setattr(
            podrun_mod, 'parse_devcontainer_json', lambda path: json.loads(dc_file.read_text())
        )

        r = parse_args(['run'])
        r = resolve_config(r)
        assert len(calls) == 2
        assert r.ns.get('run.session') is True
        assert r.ns.get('run.shell') == '/bin/zsh'

    def test_dc_scripts_then_cli_scripts(self, monkeypatch, tmp_project):
        """devcontainer configScript runs first, then CLI --config-script.

        Processing order:
            dc configScript-0, dc configScript-1, cli script-0, cli script-1
        Later scripts override earlier ones for podrun flags.
        """
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text(
            json.dumps(
                {
                    'image': 'alpine',
                    'customizations': {
                        'podrun': {
                            'configScript': ['/dc-a.sh', '/dc-b.sh'],
                        }
                    },
                }
            )
        )
        calls = []

        def fake_run_os_cmd(cmd):
            calls.append(cmd)
            outputs = {
                1: '--shell /bin/dc-a',  # dc script 0
                2: '--shell /bin/dc-b',  # dc script 1 overrides dc-a
                3: '--session',  # cli script 0
                4: '--shell /bin/cli-b',  # cli script 1 overrides dc-b
            }
            return subprocess.CompletedProcess(
                args='',
                returncode=0,
                stdout=outputs.get(len(calls), ''),
            )

        monkeypatch.setattr(podrun_mod, 'run_os_cmd', fake_run_os_cmd)
        monkeypatch.setattr(podrun_mod, 'find_devcontainer_json', lambda start_dir=None: dc_file)
        monkeypatch.setattr(
            podrun_mod, 'parse_devcontainer_json', lambda path: json.loads(dc_file.read_text())
        )

        r = parse_args(
            [
                '--config-script',
                '/cli-a.sh',
                '--config-script',
                '/cli-b.sh',
                'run',
            ]
        )
        r = resolve_config(r)
        # All 4 scripts ran in order
        assert len(calls) == 4
        # CLI script's --shell wins (last writer wins via concatenated tokens)
        assert r.ns.get('run.shell') == '/bin/cli-b'
        # --session from cli-a.sh is present
        assert r.ns.get('run.session') is True

    def test_overlay_implication_adhoc(self, monkeypatch):
        """adhoc implies session → host+interactive → user."""
        r = self._resolve(
            ['--no-devconfig', 'run', '--adhoc', 'alpine'],
            monkeypatch,
        )
        assert r.ns.get('run.adhoc') is True
        assert r.ns.get('run.session') is True
        assert r.ns.get('run.host_overlay') is True
        assert r.ns.get('run.interactive_overlay') is True
        assert r.ns.get('run.user_overlay') is True

    def test_overlay_implication_session(self, monkeypatch):
        """session implies host+interactive → user."""
        r = self._resolve(
            ['--no-devconfig', 'run', '--session', 'alpine'],
            monkeypatch,
        )
        assert r.ns.get('run.session') is True
        assert r.ns.get('run.host_overlay') is True
        assert r.ns.get('run.interactive_overlay') is True
        assert r.ns.get('run.user_overlay') is True

    def test_overlay_implication_host(self, monkeypatch):
        """host-overlay implies user."""
        r = self._resolve(
            ['--no-devconfig', 'run', '--host-overlay', 'alpine'],
            monkeypatch,
        )
        assert r.ns.get('run.host_overlay') is True
        assert r.ns.get('run.user_overlay') is True

    def test_image_resolution_cli_wins(self, monkeypatch, tmp_project):
        """CLI trailing image wins over devcontainer image."""
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text(json.dumps({'image': 'dc-image'}))
        r = self._resolve(['run', 'cli-image'], monkeypatch, dc_json_path=dc_file)
        assert r.trailing_args[0] == 'cli-image'

    def test_image_resolution_dc_fallback(self, monkeypatch, tmp_project):
        """devcontainer image used when no CLI trailing args."""
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text(json.dumps({'image': 'dc-image'}))
        r = self._resolve(['run'], monkeypatch, dc_json_path=dc_file)
        assert r.trailing_args == ['dc-image']

    def test_exports_append(self, monkeypatch, tmp_project):
        """Exports: dc + script + cli, concatenated."""
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text(
            json.dumps(
                {
                    'image': 'alpine',
                    'customizations': {'podrun': {'exports': ['/dc:/dc']}},
                }
            )
        )
        r = self._resolve(
            ['--config-script', '/s.sh', 'run', '--export', '/cli:/cli', 'alpine'],
            monkeypatch,
            dc_json_path=dc_file,
            script_stdout='--export /script:/script',
        )
        exports = r.ns.get('run.export') or []
        assert '/dc:/dc' in exports
        assert '/script:/script' in exports
        assert '/cli:/cli' in exports
        # Order: dc first, then script, then cli
        assert exports.index('/dc:/dc') < exports.index('/script:/script')
        assert exports.index('/script:/script') < exports.index('/cli:/cli')

    def test_exports_tilde_expanded(self, monkeypatch, tmp_project):
        """Tilde in export specs is expanded during resolve_config."""
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text(
            json.dumps(
                {
                    'image': 'alpine',
                    'customizations': {'podrun': {'exports': ['~/.aws:.config/.aws']}},
                }
            )
        )
        r = self._resolve(
            ['run', '--export', '~/.ssh:.config/.ssh', 'alpine'],
            monkeypatch,
            dc_json_path=dc_file,
        )
        exports = r.ns.get('run.export') or []
        # Container paths should be expanded from ~ to /home/<user>
        assert any(e.startswith(f'/home/{podrun_mod.UNAME}/.aws:') for e in exports)
        assert any(e.startswith(f'/home/{podrun_mod.UNAME}/.ssh:') for e in exports)
        # No unexpanded tildes should remain in container paths
        assert not any(e.startswith('~/') for e in exports)

    def test_workspace_folder_from_devcontainer(self, monkeypatch, tmp_project):
        """Top-level workspaceFolder from devcontainer.json is picked up."""
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text(json.dumps({'image': 'alpine', 'workspaceFolder': '/workspace'}))
        r = self._resolve(['run', 'alpine'], monkeypatch, dc_json_path=dc_file)
        assert r.ns['dc.workspace_folder'] == '/workspace'

    def test_workspace_folder_default_without_devcontainer(self, monkeypatch):
        """Without devcontainer.json, workspace_folder stays unset (default applied later)."""
        r = self._resolve(['--no-devconfig', 'run', 'alpine'], monkeypatch)
        # resolve_config doesn't set the /app default; _handle_run does
        assert r.ns.get('dc.workspace_folder') is None

    def test_remote_env_from_devcontainer(self, monkeypatch, tmp_project):
        """Top-level remoteEnv from devcontainer.json is picked up."""
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text(
            json.dumps(
                {
                    'image': 'alpine',
                    'remoteEnv': {'FOO': 'bar', 'BAZ': 'qux'},
                }
            )
        )
        r = self._resolve(['run', 'alpine'], monkeypatch, dc_json_path=dc_file)
        assert r.ns['run.remote_env'] == {'FOO': 'bar', 'BAZ': 'qux'}

    def test_store_not_autodiscovered_in_resolve_config(self, monkeypatch):
        """Store auto-discovery is handled by _resolve_store, not resolve_config."""
        r = self._resolve(
            ['--no-devconfig', 'run', 'alpine'],
            monkeypatch,
        )
        # resolve_config no longer sets root.local_store — _apply_store does
        assert r.ns.get('root.local_store') is None

    def test_label_based_dc_selection(self, monkeypatch, tmp_project):
        """Label devcontainer.config_file=<path> selects devcontainer.json."""
        dc_file = tmp_project / 'custom.json'
        dc_file.write_text(
            json.dumps(
                {
                    'image': 'custom-image',
                    'customizations': {'podrun': {'shell': '/bin/custom'}},
                }
            )
        )
        monkeypatch.setattr(podrun_mod, 'find_devcontainer_json', lambda start_dir=None: None)

        r = parse_args(
            [
                'run',
                '-l',
                f'devcontainer.config_file={dc_file}',
                'alpine',
            ]
        )
        r = resolve_config(r)
        assert r.ns.get('run.shell') == '/bin/custom'

    def test_ignore_store_flag_parsed(self, monkeypatch):
        """--local-store-ignore is parsed correctly by resolve_config."""
        r = self._resolve(
            ['--local-store-ignore', '--no-devconfig', 'run', 'alpine'],
            monkeypatch,
        )
        assert r.ns['root.local_store_ignore'] is True
        assert r.ns.get('root.local_store') is None

    def test_no_devconfig(self, monkeypatch, tmp_project):
        """--no-devconfig skips devcontainer loading."""
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text(
            json.dumps(
                {
                    'image': 'dc-image',
                    'customizations': {'podrun': {'adhoc': True}},
                }
            )
        )
        r = self._resolve(
            ['--no-devconfig', 'run', 'alpine'],
            monkeypatch,
            dc_json_path=dc_file,
        )
        # adhoc from devcontainer should NOT be applied
        assert r.ns.get('run.adhoc') is None

    def test_dc_run_args_prepended(self, monkeypatch, tmp_project):
        """devcontainer run args are prepended before CLI passthrough."""
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text(
            json.dumps(
                {
                    'image': 'alpine',
                    'capAdd': ['SYS_PTRACE'],
                    'runArgs': ['--rm'],
                }
            )
        )
        r = self._resolve(
            ['run', '-e', 'FOO=bar', 'alpine'],
            monkeypatch,
            dc_json_path=dc_file,
        )
        pt = r.ns.get('run.passthrough_args') or []
        # DC args should come before CLI args
        cap_idx = pt.index('--cap-add=SYS_PTRACE')
        e_idx = pt.index('-e')
        assert cap_idx < e_idx

    def test_script_passthrough_prepended(self, monkeypatch, tmp_project):
        """Script passthrough args prepended before CLI passthrough."""
        r = self._resolve(
            ['--config-script', '/s.sh', '--no-devconfig', 'run', '-e', 'CLI=1', 'alpine'],
            monkeypatch,
            script_stdout='-e SCRIPT=1',
        )
        pt = r.ns.get('run.passthrough_args') or []
        # Script args should come before CLI args
        script_idx = pt.index('SCRIPT=1')
        cli_idx = pt.index('CLI=1')
        assert script_idx < cli_idx

    def test_dc_before_script_before_cli_ordering(self, monkeypatch, tmp_project):
        """Passthrough ordering: DC < script < CLI (lowest to highest priority)."""
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text(
            json.dumps(
                {
                    'image': 'alpine',
                    'capAdd': ['SYS_PTRACE'],
                }
            )
        )
        r = self._resolve(
            ['--config-script', '/s.sh', 'run', '-e', 'CLI=1', 'alpine'],
            monkeypatch,
            dc_json_path=dc_file,
            script_stdout='-e SCRIPT=1',
        )
        pt = r.ns.get('run.passthrough_args') or []
        cap_idx = pt.index('--cap-add=SYS_PTRACE')
        script_idx = pt.index('SCRIPT=1')
        cli_idx = pt.index('CLI=1')
        assert cap_idx < script_idx
        assert script_idx < cli_idx

    def test_build_run_command_with_labels(self, monkeypatch):
        """Labels from run.label are forwarded in build_run_command."""
        r = self._resolve(
            ['--no-devconfig', 'run', '-l', 'app=test', '-l', 'env=dev', 'alpine'],
            monkeypatch,
        )
        cmd = build_run_command(r, 'podman')
        assert '--label=app=test' in cmd
        assert '--label=env=dev' in cmd

    def test_passthrough_subcommand_not_affected(self, monkeypatch):
        """resolve_config doesn't break passthrough subcommands."""
        r = self._resolve(
            ['--no-devconfig', 'ps', '-a'],
            monkeypatch,
        )
        assert r.ns['subcommand'] == 'ps'


# ---------------------------------------------------------------------------
# TestIntegrationPipeline — resolve_config → build_run_command end-to-end
# ---------------------------------------------------------------------------


class TestIntegrationPipeline:
    """End-to-end tests: parse → resolve_config → build_run_command.

    Each test verifies that config sources (devcontainer.json, config scripts,
    CLI flags) produce the correct final podman command line.
    """

    def _cmd(self, argv, monkeypatch, dc_file=None, script_effects=None):
        """Parse + resolve + build, returning the final command list.

        Args:
            argv: CLI args (without podman path).
            dc_file: Path to a real devcontainer.json on disk (in tmp_path).
            script_effects: List of (returncode, stdout) tuples for successive
                            run_os_cmd calls, or None for no mocking.
        """
        monkeypatch.setattr(
            podrun_mod,
            'find_devcontainer_json',
            lambda start_dir=None: dc_file,
        )

        if script_effects is not None:
            call_idx = [0]

            def fake_run_os_cmd(cmd):
                i = call_idx[0]
                call_idx[0] += 1
                if i < len(script_effects):
                    rc, stdout = script_effects[i]
                    return subprocess.CompletedProcess(
                        args='', returncode=rc, stdout=stdout, stderr=''
                    )
                return subprocess.CompletedProcess(args='', returncode=0, stdout='', stderr='')

            monkeypatch.setattr(podrun_mod, 'run_os_cmd', fake_run_os_cmd)

        r = parse_args(argv)
        r = resolve_config(r)
        return build_run_command(r, 'podman')

    def _write_dc(self, tmp_path, dc):
        """Write devcontainer.json and return its path."""
        dc_dir = tmp_path / '.devcontainer'
        dc_dir.mkdir(exist_ok=True)
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text(json.dumps(dc))
        return dc_file

    # -- DC-only command building ---------------------------------------------

    def test_dc_mounts_in_command(self, monkeypatch, tmp_project):
        dc_file = self._write_dc(
            tmp_project,
            {
                'image': 'alpine',
                'mounts': ['type=bind,src=/host,dst=/ctr'],
            },
        )
        cmd = self._cmd(['run'], monkeypatch, dc_file=dc_file)
        assert '--mount=type=bind,src=/host,dst=/ctr' in cmd
        assert cmd[-1] == 'alpine'

    def test_dc_mounts_dict_in_command(self, monkeypatch, tmp_project):
        dc_file = self._write_dc(
            tmp_project,
            {
                'image': 'alpine',
                'mounts': [{'type': 'bind', 'src': '/a', 'dst': '/b'}],
            },
        )
        cmd = self._cmd(['run'], monkeypatch, dc_file=dc_file)
        assert '--mount=type=bind,src=/a,dst=/b' in cmd

    def test_dc_cap_add_in_command(self, monkeypatch, tmp_project):
        dc_file = self._write_dc(
            tmp_project,
            {
                'image': 'alpine',
                'capAdd': ['SYS_PTRACE', 'NET_ADMIN'],
            },
        )
        cmd = self._cmd(['run'], monkeypatch, dc_file=dc_file)
        assert '--cap-add=SYS_PTRACE' in cmd
        assert '--cap-add=NET_ADMIN' in cmd

    def test_dc_security_opt_in_command(self, monkeypatch, tmp_project):
        dc_file = self._write_dc(
            tmp_project,
            {
                'image': 'alpine',
                'securityOpt': ['seccomp=unconfined'],
            },
        )
        cmd = self._cmd(['run'], monkeypatch, dc_file=dc_file)
        assert '--security-opt=seccomp=unconfined' in cmd

    def test_dc_privileged_in_command(self, monkeypatch, tmp_project):
        dc_file = self._write_dc(
            tmp_project,
            {
                'image': 'alpine',
                'privileged': True,
            },
        )
        cmd = self._cmd(['run'], monkeypatch, dc_file=dc_file)
        assert '--privileged' in cmd

    def test_dc_init_in_command(self, monkeypatch, tmp_project):
        dc_file = self._write_dc(
            tmp_project,
            {
                'image': 'alpine',
                'init': True,
            },
        )
        cmd = self._cmd(['run'], monkeypatch, dc_file=dc_file)
        assert '--init' in cmd

    def test_dc_run_args_in_command(self, monkeypatch, tmp_project):
        dc_file = self._write_dc(
            tmp_project,
            {
                'image': 'alpine',
                'runArgs': ['--rm', '--network=host'],
            },
        )
        cmd = self._cmd(['run'], monkeypatch, dc_file=dc_file)
        assert '--rm' in cmd
        assert '--network=host' in cmd

    def test_dc_image_fallback_in_command(self, monkeypatch, tmp_project):
        """DC image used when CLI provides no trailing args."""
        dc_file = self._write_dc(tmp_project, {'image': 'ubuntu:22.04'})
        cmd = self._cmd(['run'], monkeypatch, dc_file=dc_file)
        assert cmd[-1] == 'ubuntu:22.04'

    def test_dc_image_overridden_by_cli(self, monkeypatch, tmp_project):
        """CLI image wins over DC image."""
        dc_file = self._write_dc(tmp_project, {'image': 'dc-image'})
        cmd = self._cmd(['run', 'cli-image'], monkeypatch, dc_file=dc_file)
        assert 'cli-image' in cmd
        assert 'dc-image' not in cmd

    def test_dc_combined_fields_in_command(self, monkeypatch, tmp_project):
        """Multiple DC fields all appear in the final command."""
        dc_file = self._write_dc(
            tmp_project,
            {
                'image': 'alpine',
                'mounts': ['type=tmpfs,dst=/tmp'],
                'capAdd': ['SYS_PTRACE'],
                'securityOpt': ['seccomp=unconfined'],
                'privileged': True,
                'init': True,
                'runArgs': ['--rm'],
            },
        )
        cmd = self._cmd(['run'], monkeypatch, dc_file=dc_file)
        assert '--mount=type=tmpfs,dst=/tmp' in cmd
        assert '--cap-add=SYS_PTRACE' in cmd
        assert '--security-opt=seccomp=unconfined' in cmd
        assert '--privileged' in cmd
        assert '--init' in cmd
        assert '--rm' in cmd
        assert cmd[-1] == 'alpine'

    # -- Script passthrough in command ----------------------------------------

    def test_script_passthrough_in_command(self, monkeypatch, tmp_project):
        """Config script output becomes podman flags in the final command."""
        cmd = self._cmd(
            ['--config-script', '/s.sh', '--no-devconfig', 'run', 'alpine'],
            monkeypatch,
            script_effects=[(0, '-e SCRIPT_VAR=1 --rm')],
        )
        assert '-e' in cmd
        assert 'SCRIPT_VAR=1' in cmd
        assert '--rm' in cmd
        assert cmd[-1] == 'alpine'

    def test_script_name_in_command(self, monkeypatch, tmp_project):
        """Config script setting --name flows to --name= in the final command."""
        cmd = self._cmd(
            ['--config-script', '/s.sh', '--no-devconfig', 'run', 'alpine'],
            monkeypatch,
            script_effects=[(0, '--name from-script')],
        )
        assert '--name=from-script' in cmd

    # -- Three-way merge in final command -------------------------------------

    def test_dc_args_before_cli_passthrough(self, monkeypatch, tmp_project):
        """DC run args appear before CLI passthrough args in final command."""
        dc_file = self._write_dc(
            tmp_project,
            {
                'image': 'alpine',
                'capAdd': ['SYS_PTRACE'],
            },
        )
        cmd = self._cmd(
            ['run', '-e', 'CLI=1', 'alpine'],
            monkeypatch,
            dc_file=dc_file,
        )
        cap_idx = cmd.index('--cap-add=SYS_PTRACE')
        e_idx = cmd.index('-e')
        assert cap_idx < e_idx

    def test_script_args_before_cli_passthrough(self, monkeypatch, tmp_project):
        """Script passthrough args appear before CLI passthrough args."""
        cmd = self._cmd(
            ['--config-script', '/s.sh', '--no-devconfig', 'run', '-e', 'CLI=1', 'alpine'],
            monkeypatch,
            script_effects=[(0, '-e SCRIPT=1')],
        )
        script_e_idx = cmd.index('SCRIPT=1') - 1  # -e before SCRIPT=1
        cli_e_idx = cmd.index('CLI=1') - 1  # -e before CLI=1
        assert script_e_idx < cli_e_idx

    def test_dc_args_before_script_before_cli(self, monkeypatch, tmp_project):
        """Ordering: DC run args (lowest), then script, then CLI (highest).

        Podman uses last-writer-wins, so this ordering means CLI overrides
        script which overrides DC.
        """
        dc_file = self._write_dc(
            tmp_project,
            {
                'image': 'alpine',
                'capAdd': ['SYS_PTRACE'],
                'customizations': {'podrun': {'configScript': '/dc-script.sh'}},
            },
        )
        cmd = self._cmd(
            ['--config-script', '/cli-script.sh', 'run', '-e', 'CLI=1', 'alpine'],
            monkeypatch,
            dc_file=dc_file,
            script_effects=[
                (0, '-e DC_SCRIPT=1'),  # dc configScript
                (0, '-e CLI_SCRIPT=1'),  # cli --config-script
            ],
        )
        # All three sources contribute flags; verify ordering
        cap_idx = cmd.index('--cap-add=SYS_PTRACE')
        dc_script_idx = cmd.index('DC_SCRIPT=1')
        cli_script_idx = cmd.index('CLI_SCRIPT=1')
        cli_idx = cmd.index('CLI=1')
        # DC run args < script passthrough < CLI passthrough
        assert cap_idx < dc_script_idx
        assert dc_script_idx < cli_script_idx
        assert cli_script_idx < cli_idx

    def test_cli_name_overrides_script_name(self, monkeypatch, tmp_project):
        """CLI --name wins over script --name in the final command."""
        cmd = self._cmd(
            ['--config-script', '/s.sh', '--no-devconfig', 'run', '--name', 'cli-name', 'alpine'],
            monkeypatch,
            script_effects=[(0, '--name script-name')],
        )
        assert '--name=cli-name' in cmd
        assert '--name=script-name' not in cmd

    def test_cli_name_overrides_dc_name(self, monkeypatch, tmp_project):
        """CLI --name wins over devcontainer name in the final command."""
        dc_file = self._write_dc(
            tmp_project,
            {
                'image': 'alpine',
                'customizations': {'podrun': {'name': 'dc-name'}},
            },
        )
        cmd = self._cmd(
            ['run', '--name', 'cli-name', 'alpine'],
            monkeypatch,
            dc_file=dc_file,
        )
        assert '--name=cli-name' in cmd
        assert '--name=dc-name' not in cmd

    def test_script_name_overrides_dc_name(self, monkeypatch, tmp_project):
        """Script --name wins over devcontainer name in the final command."""
        dc_file = self._write_dc(
            tmp_project,
            {
                'image': 'alpine',
                'customizations': {
                    'podrun': {
                        'name': 'dc-name',
                        'configScript': '/s.sh',
                    }
                },
            },
        )
        cmd = self._cmd(
            ['run'],
            monkeypatch,
            dc_file=dc_file,
            script_effects=[(0, '--name script-name')],
        )
        assert '--name=script-name' in cmd
        assert '--name=dc-name' not in cmd

    # -- Labels in final command ----------------------------------------------

    def test_labels_with_dc_args(self, monkeypatch, tmp_project):
        """Labels and DC args both appear in the final command."""
        dc_file = self._write_dc(
            tmp_project,
            {
                'image': 'alpine',
                'capAdd': ['SYS_PTRACE'],
            },
        )
        cmd = self._cmd(
            ['run', '-l', 'app=test', 'alpine'],
            monkeypatch,
            dc_file=dc_file,
        )
        assert '--label=app=test' in cmd
        assert '--cap-add=SYS_PTRACE' in cmd

    # -- Overlay flags don't produce podman args yet (Phase 2) ----------------

    def test_overlay_flags_no_command_side_effects(self, monkeypatch, tmp_project):
        """Overlay flags resolve but don't add podman args in Phase 1."""
        cmd = self._cmd(
            ['--no-devconfig', 'run', '--adhoc', 'alpine'],
            monkeypatch,
        )
        # The command should just be podman run alpine — overlays don't
        # translate to podman flags yet.
        assert cmd == ['podman', 'run', 'alpine']

    # -- no-devconfig suppresses DC in final command --------------------------

    def test_no_devconfig_excludes_dc_from_command(self, monkeypatch, tmp_project):
        """--no-devconfig prevents DC mounts/caps from appearing in command."""
        dc_file = self._write_dc(
            tmp_project,
            {
                'image': 'dc-image',
                'capAdd': ['SYS_PTRACE'],
                'mounts': ['type=bind,src=/a,dst=/b'],
            },
        )
        cmd = self._cmd(
            ['--no-devconfig', 'run', 'alpine'],
            monkeypatch,
            dc_file=dc_file,
        )
        assert '--cap-add=SYS_PTRACE' not in cmd
        assert '--mount=type=bind,src=/a,dst=/b' not in cmd
        assert 'dc-image' not in cmd
        assert cmd[-1] == 'alpine'

    # -- Global podman flags with config sources ------------------------------

    def test_global_flags_with_dc_args(self, monkeypatch, tmp_project, podman_only):
        """Global podman flags appear before 'run', DC args after."""
        dc_file = self._write_dc(
            tmp_project,
            {
                'image': 'alpine',
                'capAdd': ['SYS_PTRACE'],
            },
        )
        cmd = self._cmd(
            ['--remote', 'run', 'alpine'],
            monkeypatch,
            dc_file=dc_file,
        )
        run_idx = cmd.index('run')
        remote_idx = cmd.index('--remote')
        cap_idx = cmd.index('--cap-add=SYS_PTRACE')
        assert remote_idx < run_idx
        assert cap_idx > run_idx

    # -- Complex real-world scenario ------------------------------------------

    def test_full_scenario(self, monkeypatch, tmp_project):
        """Real-world-like scenario: DC + script + CLI all contributing."""
        dc_file = self._write_dc(
            tmp_project,
            {
                'image': 'dc-image:latest',
                'mounts': [{'type': 'bind', 'src': '/data', 'dst': '/data'}],
                'capAdd': ['SYS_PTRACE'],
                'init': True,
                'runArgs': ['--network=host'],
                'customizations': {
                    'podrun': {
                        'name': 'dc-name',
                        'configScript': '/dc-script.sh',
                    }
                },
            },
        )
        cmd = self._cmd(
            [
                '--config-script',
                '/cli-script.sh',
                'run',
                '--name',
                'my-container',
                '-l',
                'env=prod',
                '-e',
                'CLI_VAR=1',
                '-v',
                '/host:/ctr',
                'my-image:v2',
            ],
            monkeypatch,
            dc_file=dc_file,
            script_effects=[
                (0, '-e DC_SCRIPT_VAR=1'),  # dc configScript
                (0, '-e CLI_SCRIPT_VAR=1 --rm'),  # cli --config-script
            ],
        )

        # Structure: podman run --name=... --label=... [passthrough] image
        assert cmd[0] == 'podman'
        assert cmd[1] == 'run'

        # CLI --name wins over dc name
        assert '--name=my-container' in cmd
        assert '--name=dc-name' not in cmd

        # Labels forwarded
        assert '--label=env=prod' in cmd

        # DC run args present
        assert '--mount=type=bind,src=/data,dst=/data' in cmd
        assert '--cap-add=SYS_PTRACE' in cmd
        assert '--init' in cmd
        assert '--network=host' in cmd

        # Script passthrough present
        assert 'DC_SCRIPT_VAR=1' in cmd
        assert 'CLI_SCRIPT_VAR=1' in cmd
        assert '--rm' in cmd

        # CLI passthrough present
        assert 'CLI_VAR=1' in cmd
        assert '/host:/ctr' in cmd

        # CLI image wins over DC image
        assert cmd[-1] == 'my-image:v2'
        assert 'dc-image:latest' not in cmd

        # Ordering: DC run args < script passthrough < CLI passthrough
        mount_idx = cmd.index('--mount=type=bind,src=/data,dst=/data')
        dc_script_idx = cmd.index('DC_SCRIPT_VAR=1')
        cli_script_idx = cmd.index('CLI_SCRIPT_VAR=1')
        cli_var_idx = cmd.index('CLI_VAR=1')
        assert mount_idx < dc_script_idx
        assert dc_script_idx < cli_script_idx
        assert cli_script_idx < cli_var_idx


# ---------------------------------------------------------------------------
# TestExpandDevcontainerVars — variable expansion
# ---------------------------------------------------------------------------


class TestExpandDevcontainerVars:
    def test_expand_local_workspace_folder(self):
        ctx = {'localWorkspaceFolder': '/home/user/project', 'containerWorkspaceFolder': ''}
        result = _expand_devcontainer_vars('${localWorkspaceFolder}/src', ctx)
        assert result == '/home/user/project/src'

    def test_expand_local_workspace_folder_basename(self):
        ctx = {'localWorkspaceFolder': '/home/user/project', 'containerWorkspaceFolder': ''}
        result = _expand_devcontainer_vars('${localWorkspaceFolderBasename}', ctx)
        assert result == 'project'

    def test_expand_container_workspace_folder(self):
        ctx = {'localWorkspaceFolder': '', 'containerWorkspaceFolder': '/workspaces/myproj'}
        result = _expand_devcontainer_vars('${containerWorkspaceFolder}', ctx)
        assert result == '/workspaces/myproj'

    def test_expand_container_workspace_folder_basename(self):
        ctx = {'localWorkspaceFolder': '', 'containerWorkspaceFolder': '/workspaces/myproj'}
        result = _expand_devcontainer_vars('${containerWorkspaceFolderBasename}', ctx)
        assert result == 'myproj'

    def test_expand_local_env(self, monkeypatch):
        monkeypatch.setenv('PODRUN_TEST_VAR_XYZ', 'hello')
        ctx = {'localWorkspaceFolder': '', 'containerWorkspaceFolder': ''}
        result = _expand_devcontainer_vars('${localEnv:PODRUN_TEST_VAR_XYZ}', ctx)
        assert result == 'hello'

    def test_expand_local_env_default(self, monkeypatch):
        monkeypatch.delenv('PODRUN_MISSING_VAR_XYZ', raising=False)
        ctx = {'localWorkspaceFolder': '', 'containerWorkspaceFolder': ''}
        result = _expand_devcontainer_vars('${localEnv:PODRUN_MISSING_VAR_XYZ:fallback}', ctx)
        assert result == 'fallback'

    def test_expand_local_env_missing_no_default(self, monkeypatch):
        monkeypatch.delenv('PODRUN_MISSING_VAR_XYZ', raising=False)
        ctx = {'localWorkspaceFolder': '', 'containerWorkspaceFolder': ''}
        result = _expand_devcontainer_vars('${localEnv:PODRUN_MISSING_VAR_XYZ}', ctx)
        assert result == ''

    def test_expand_container_env_passthrough(self):
        ctx = {'localWorkspaceFolder': '', 'containerWorkspaceFolder': ''}
        result = _expand_devcontainer_vars('${containerEnv:PATH}', ctx)
        assert result == '${containerEnv:PATH}'

    def test_expand_devcontainer_id(self):
        ctx = {'localWorkspaceFolder': '/home/user/project', 'containerWorkspaceFolder': ''}
        result = _expand_devcontainer_vars('${devcontainerId}', ctx)
        import hashlib

        expected = hashlib.sha256(b'/home/user/project').hexdigest()[:16]
        assert result == expected

    def test_expand_nested_in_dict(self):
        ctx = {'localWorkspaceFolder': '/proj', 'containerWorkspaceFolder': ''}
        result = _expand_devcontainer_vars({'key': '${localWorkspaceFolder}/dir'}, ctx)
        assert result == {'key': '/proj/dir'}

    def test_expand_nested_in_list(self):
        ctx = {'localWorkspaceFolder': '/proj', 'containerWorkspaceFolder': ''}
        result = _expand_devcontainer_vars(['${localWorkspaceFolder}/a', 'plain'], ctx)
        assert result == ['/proj/a', 'plain']

    def test_expand_no_vars(self):
        ctx = {'localWorkspaceFolder': '/proj', 'containerWorkspaceFolder': ''}
        result = _expand_devcontainer_vars('no variables here', ctx)
        assert result == 'no variables here'

    def test_expand_non_string(self):
        ctx = {'localWorkspaceFolder': '/proj', 'containerWorkspaceFolder': ''}
        assert _expand_devcontainer_vars(42, ctx) == 42
        assert _expand_devcontainer_vars(True, ctx) is True
        assert _expand_devcontainer_vars(None, ctx) is None

    def test_expand_unknown_var_left_as_is(self):
        ctx = {'localWorkspaceFolder': '', 'containerWorkspaceFolder': ''}
        result = _expand_devcontainer_vars('${unknownVar}', ctx)
        assert result == '${unknownVar}'


# ---------------------------------------------------------------------------
# TestDevcontainerProjectDir
# ---------------------------------------------------------------------------


class TestDevcontainerProjectDir:
    def test_standard_location(self, tmp_path):
        dc_dir = tmp_path / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text('{}')
        assert _devcontainer_project_dir(str(dc_file)) == str(tmp_path)

    def test_named_config(self, tmp_path):
        dc_dir = tmp_path / '.devcontainer' / 'myconfig'
        dc_dir.mkdir(parents=True)
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text('{}')
        assert _devcontainer_project_dir(str(dc_file)) == str(tmp_path)

    def test_shorthand(self, tmp_path):
        dc_file = tmp_path / '.devcontainer.json'
        dc_file.write_text('{}')
        assert _devcontainer_project_dir(str(dc_file)) == str(tmp_path)

    def test_explicit_path(self, tmp_path):
        dc_file = tmp_path / 'custom.json'
        dc_file.write_text('{}')
        assert _devcontainer_project_dir(str(dc_file)) == str(tmp_path)

    def test_none(self):
        assert _devcontainer_project_dir(None) is None


# ---------------------------------------------------------------------------
# TestWorkspaceMount — workspaceMount parsing
# ---------------------------------------------------------------------------


class TestWorkspaceMount:
    def _resolve(self, argv, monkeypatch, dc=None, dc_json_path=None):
        monkeypatch.setattr(
            podrun_mod, 'find_devcontainer_json', lambda start_dir=None: dc_json_path
        )
        if dc is not None:
            monkeypatch.setattr(podrun_mod, 'parse_devcontainer_json', lambda path: dc)
        result = parse_args(argv)
        return resolve_config(result)

    def test_workspace_mount_parsed(self, monkeypatch, tmp_project):
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text(
            json.dumps(
                {
                    'image': 'alpine',
                    'workspaceMount': 'source=/host/proj,target=/workspace,type=bind',
                }
            )
        )
        r = self._resolve(['run'], monkeypatch, dc_json_path=dc_file)
        assert r.ns.get('dc.workspace_mount') == 'source=/host/proj,target=/workspace,type=bind'
        assert r.ns.get('dc.workspace_folder') == '/workspace'

    def test_workspace_mount_empty_disables(self, monkeypatch, tmp_project):
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text(json.dumps({'image': 'alpine', 'workspaceMount': ''}))
        r = self._resolve(['run'], monkeypatch, dc_json_path=dc_file)
        assert r.ns.get('dc.workspace_mount') == ''

    def test_workspace_mount_variables_expanded(self, monkeypatch, tmp_project):
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text(
            json.dumps(
                {
                    'image': 'alpine',
                    'workspaceMount': (
                        'source=${localWorkspaceFolder},target=/workspace,type=bind'
                    ),
                }
            )
        )
        r = self._resolve(['run'], monkeypatch, dc_json_path=dc_file)
        mount = r.ns.get('dc.workspace_mount') or ''
        assert '${' not in mount
        assert f'source={tmp_project}' in mount

    def test_workspace_folder_fallback(self, monkeypatch, tmp_project):
        """No workspaceMount → existing workspaceFolder behavior."""
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text(json.dumps({'image': 'alpine', 'workspaceFolder': '/myworkspace'}))
        r = self._resolve(['run'], monkeypatch, dc_json_path=dc_file)
        assert r.ns.get('dc.workspace_folder') == '/myworkspace'
        assert r.ns.get('dc.workspace_mount') is None

    def test_workspace_mount_target_overrides_workspace_folder(self, monkeypatch, tmp_project):
        """workspaceMount target takes priority over workspaceFolder."""
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text(
            json.dumps(
                {
                    'image': 'alpine',
                    'workspaceFolder': '/ignored',
                    'workspaceMount': 'source=/host,target=/from-mount,type=bind',
                }
            )
        )
        r = self._resolve(['run'], monkeypatch, dc_json_path=dc_file)
        assert r.ns.get('dc.workspace_folder') == '/from-mount'

    def test_workspace_mount_no_target_falls_through(self, monkeypatch, tmp_project):
        """workspaceMount without target= falls through to workspaceFolder."""
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text(
            json.dumps(
                {
                    'image': 'alpine',
                    'workspaceFolder': '/from-folder',
                    'workspaceMount': 'source=/host,type=bind',
                }
            )
        )
        r = self._resolve(['run'], monkeypatch, dc_json_path=dc_file)
        # No target in workspaceMount → workspaceFolder wins
        assert r.ns.get('dc.workspace_folder') == '/from-folder'


# ---------------------------------------------------------------------------
# TestDevcontainerRunArgsContainerEnv — containerEnv support
# ---------------------------------------------------------------------------


class TestDevcontainerRunArgsContainerEnv:
    def test_container_env(self):
        dc = {'containerEnv': {'FOO': 'bar', 'BAZ': 'qux'}}
        args = devcontainer_run_args(dc, {})
        assert '--env=FOO=bar' in args
        assert '--env=BAZ=qux' in args

    def test_container_env_empty(self):
        dc = {'containerEnv': {}}
        args = devcontainer_run_args(dc, {})
        assert not any(a.startswith('--env=') for a in args)

    def test_container_env_absent(self):
        dc = {}
        args = devcontainer_run_args(dc, {})
        assert not any(a.startswith('--env=') for a in args)


# ---------------------------------------------------------------------------
# TestDevcontainerCliDetection — skip dc→args when devcontainer CLI drives
# ---------------------------------------------------------------------------


class TestDevcontainerCliDetection:
    def _resolve(self, argv, monkeypatch, dc=None, dc_json_path=None):
        monkeypatch.setattr(
            podrun_mod, 'find_devcontainer_json', lambda start_dir=None: dc_json_path
        )
        if dc is not None:
            monkeypatch.setattr(podrun_mod, 'parse_devcontainer_json', lambda path: dc)
        result = parse_args(argv)
        return resolve_config(result)

    def test_dc_run_args_skipped_with_label(self, monkeypatch, tmp_project):
        """When devcontainer CLI drives, dc fields are NOT re-emitted as podman args."""
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc = {
            'image': 'alpine',
            'workspaceMount': 'source=/host,target=/app,type=bind',
            'workspaceFolder': '/app',
            'capAdd': ['SYS_PTRACE'],
        }
        dc_file.write_text(json.dumps(dc))
        # Simulate devcontainer CLI: passes mount + label in passthrough
        r = self._resolve(
            [
                'run',
                '-l',
                f'devcontainer.config_file={dc_file}',
                '--mount=source=/host,target=/app,type=bind',
                '-w=/app',
                '--cap-add=SYS_PTRACE',
                'alpine',
            ],
            monkeypatch,
        )
        pt = r.ns['run.passthrough_args']
        # Only ONE mount to /app (from CLI passthrough), not duplicated by dc
        from podrun.podrun import _volume_mount_destinations

        dests = _volume_mount_destinations(pt)
        assert '/app' in dests
        mount_count = 0
        i = 0
        while i < len(pt):
            if pt[i].startswith('--mount=') and '/app' in pt[i]:
                mount_count += 1
            elif pt[i] == '--mount' and i + 1 < len(pt) and '/app' in pt[i + 1]:
                mount_count += 1
                i += 1
            i += 1
        assert mount_count == 1

    def test_dc_run_args_emitted_without_label(self, monkeypatch, tmp_project):
        """When podrun drives directly, dc fields ARE emitted as podman args."""
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc = {
            'image': 'alpine',
            'workspaceMount': 'source=/host,target=/app,type=bind',
            'workspaceFolder': '/app',
        }
        dc_file.write_text(json.dumps(dc))
        r = self._resolve(['run', 'alpine'], monkeypatch, dc_json_path=dc_file)
        pt = r.ns['run.passthrough_args']
        assert any('--mount=' in a and '/app' in a for a in pt)
        assert any(a == '-w=/app' for a in pt)

    def test_podrun_cfg_preserved_with_label(self, monkeypatch, tmp_project):
        """When devcontainer CLI drives, podrun_cfg is still available."""
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc = {
            'image': 'alpine',
            'customizations': {'podrun': {'userOverlay': True, 'exports': ['~/.ssh:.ssh']}},
        }
        dc_file.write_text(json.dumps(dc))
        r = self._resolve(
            ['run', '-l', f'devcontainer.config_file={dc_file}', 'alpine'],
            monkeypatch,
        )
        # Overlay config from customizations.podrun should be applied
        assert r.ns.get('run.user_overlay') is True

    def test_dc_namespace_set_with_label(self, monkeypatch, tmp_project):
        """When devcontainer CLI drives, dc.* namespace values are still populated."""
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc = {
            'image': 'alpine',
            'workspaceFolder': '/workspace',
            'remoteEnv': {'FOO': 'bar'},
        }
        dc_file.write_text(json.dumps(dc))
        r = self._resolve(
            ['run', '-l', f'devcontainer.config_file={dc_file}', 'alpine'],
            monkeypatch,
        )
        # dc.* fields are populated for internal use (PODRUN_WORKDIR, etc.)
        assert r.ns.get('dc.workspace_folder') == '/workspace'
        assert r.ns.get('dc.remote_env') == {'FOO': 'bar'}
        # But the internal flag is set to suppress arg emission
        assert r.ns.get('internal.dc_from_cli') is True


# ---------------------------------------------------------------------------
# TestVariableExpansionIntegration — end-to-end with resolve_config
# ---------------------------------------------------------------------------


class TestVariableExpansionIntegration:
    def _resolve(self, argv, monkeypatch, dc_json_path=None):
        monkeypatch.setattr(
            podrun_mod, 'find_devcontainer_json', lambda start_dir=None: dc_json_path
        )
        result = parse_args(argv)
        return resolve_config(result)

    def test_workspace_folder_with_basename_var(self, monkeypatch, tmp_project):
        """workspaceFolder using ${localWorkspaceFolderBasename} is expanded."""
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text(
            json.dumps(
                {
                    'image': 'alpine',
                    'workspaceFolder': '/workspaces/${localWorkspaceFolderBasename}',
                }
            )
        )
        r = self._resolve(['run'], monkeypatch, dc_json_path=dc_file)
        expected = f'/workspaces/{tmp_project.name}'
        assert r.ns.get('dc.workspace_folder') == expected

    def test_mounts_variable_expanded(self, monkeypatch, tmp_project):
        """Variables in mounts array are expanded."""
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text(
            json.dumps(
                {
                    'image': 'alpine',
                    'mounts': ['type=bind,src=${localWorkspaceFolder}/data,dst=/data'],
                }
            )
        )
        r = self._resolve(['run'], monkeypatch, dc_json_path=dc_file)
        pt = r.ns.get('run.passthrough_args') or []
        expected_mount = f'--mount=type=bind,src={tmp_project}/data,dst=/data'
        assert expected_mount in pt

    def test_remote_env_variable_expanded(self, monkeypatch, tmp_project):
        """Variables in remoteEnv are expanded."""
        dc_dir = tmp_project / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text(
            json.dumps(
                {
                    'image': 'alpine',
                    'remoteEnv': {'PROJECT': '${localWorkspaceFolder}'},
                }
            )
        )
        r = self._resolve(['run'], monkeypatch, dc_json_path=dc_file)
        assert r.ns['run.remote_env'] == {'PROJECT': str(tmp_project)}

    def test_no_devconfig_skips_expansion(self, monkeypatch):
        """--no-devconfig produces no variable expansion errors."""
        r = self._resolve(['--no-devconfig', 'run', 'alpine'], monkeypatch)
        assert r.ns.get('dc.workspace_folder') is None
