import json

from podrun.podrun import _strip_jsonc


class TestStripJsonc:
    def test_no_comments(self):
        text = '{"key": "value"}'
        assert _strip_jsonc(text) == text

    def test_line_comment(self):
        text = '{\n  // comment\n  "key": "value"\n}'
        result = _strip_jsonc(text)
        assert '//' not in result
        assert json.loads(result) == {'key': 'value'}

    def test_block_comment(self):
        text = '{\n  /* block\n  comment */\n  "key": "value"\n}'
        result = _strip_jsonc(text)
        assert '/*' not in result
        assert json.loads(result) == {'key': 'value'}

    def test_trailing_comma_object(self):
        text = '{"a": 1, "b": 2,}'
        result = _strip_jsonc(text)
        assert json.loads(result) == {'a': 1, 'b': 2}

    def test_trailing_comma_array(self):
        text = '{"a": [1, 2, 3,]}'
        result = _strip_jsonc(text)
        assert json.loads(result) == {'a': [1, 2, 3]}

    def test_escaped_quotes_in_string(self):
        text = r'{"key": "val\"ue"}'
        result = _strip_jsonc(text)
        assert json.loads(result) == {'key': 'val"ue'}

    def test_slash_in_string_not_stripped(self):
        text = '{"url": "http://example.com"}'
        result = _strip_jsonc(text)
        assert json.loads(result) == {'url': 'http://example.com'}

    def test_combined_scenario(self):
        text = (
            '{\n'
            '  // line comment\n'
            '  "name": "test", /* inline block */\n'
            '  "items": [\n'
            '    "a",\n'
            '    "b", // trailing\n'
            '  ],\n'
            '}'
        )
        result = _strip_jsonc(text)
        parsed = json.loads(result)
        assert parsed == {'name': 'test', 'items': ['a', 'b']}

    def test_roundtrip_valid_json(self):
        original = {'nested': {'key': [1, 2, 3]}, 'str': 'hello'}
        text = json.dumps(original, indent=2)
        result = _strip_jsonc(text)
        assert json.loads(result) == original
