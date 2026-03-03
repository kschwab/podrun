import json

import pytest

from podrun.podrun import (
    extract_podrun_config,
    find_devcontainer_json,
    parse_devcontainer_json,
)


class TestFindDevcontainerJson:
    def test_found_in_start_dir(self, tmp_path):
        dc_dir = tmp_path / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text('{}')
        result = find_devcontainer_json(str(tmp_path))
        assert result == dc_file

    def test_found_in_parent(self, tmp_path):
        dc_dir = tmp_path / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text('{}')
        child = tmp_path / 'sub' / 'dir'
        child.mkdir(parents=True)
        result = find_devcontainer_json(str(child))
        assert result == dc_file

    def test_not_found(self, tmp_path):
        child = tmp_path / 'empty'
        child.mkdir()
        result = find_devcontainer_json(str(child))
        assert result is None


class TestParseDevcontainerJson:
    def test_none_returns_empty_dict(self):
        assert parse_devcontainer_json(None) == {}

    def test_plain_json(self, tmp_path):
        f = tmp_path / 'dc.json'
        f.write_text('{"image": "alpine"}')
        result = parse_devcontainer_json(f)
        assert result == {'image': 'alpine'}

    def test_jsonc_with_comments(self, tmp_path):
        f = tmp_path / 'dc.json'
        f.write_text('{\n  // comment\n  "image": "alpine",\n}')
        result = parse_devcontainer_json(f)
        assert result == {'image': 'alpine'}

    def test_invalid_json_raises(self, tmp_path):
        f = tmp_path / 'dc.json'
        f.write_text('{invalid}')
        with pytest.raises(json.JSONDecodeError):
            parse_devcontainer_json(f)


class TestExtractPodrunConfig:
    def test_with_config(self):
        dc = {'customizations': {'podrun': {'name': 'mycontainer'}}}
        assert extract_podrun_config(dc) == {'name': 'mycontainer'}

    def test_missing_customizations(self):
        assert extract_podrun_config({}) == {}

    def test_missing_podrun_key(self):
        dc = {'customizations': {'vscode': {}}}
        assert extract_podrun_config(dc) == {}
