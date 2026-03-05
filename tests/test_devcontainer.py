import json

import pytest

from podrun.podrun import (
    _find_project_context,
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


class TestFindProjectContext:
    """Tests for combined _find_project_context() discovery."""

    def test_finds_both_devcontainer_and_store(self, tmp_path):
        """_find_project_context finds both devcontainer.json and store in one pass."""
        dc_dir = tmp_path / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text('{}')
        store = dc_dir / '.podrun' / 'store'
        (store / 'graphroot').mkdir(parents=True)
        ctx = _find_project_context(start_dir=str(tmp_path))
        assert ctx.devcontainer_json == dc_file
        assert ctx.store_dir == str(store)

    def test_finds_devcontainer_without_store(self, tmp_path):
        """_find_project_context finds devcontainer.json when no store exists."""
        dc_dir = tmp_path / '.devcontainer'
        dc_dir.mkdir()
        dc_file = dc_dir / 'devcontainer.json'
        dc_file.write_text('{}')
        ctx = _find_project_context(start_dir=str(tmp_path))
        assert ctx.devcontainer_json == dc_file
        assert ctx.store_dir is None

    def test_finds_store_without_devcontainer(self, tmp_path):
        """_find_project_context finds store when no devcontainer.json exists."""
        dc_dir = tmp_path / '.devcontainer'
        store = dc_dir / '.podrun' / 'store'
        (store / 'graphroot').mkdir(parents=True)
        ctx = _find_project_context(start_dir=str(tmp_path))
        assert ctx.devcontainer_json is None
        assert ctx.store_dir == str(store)
