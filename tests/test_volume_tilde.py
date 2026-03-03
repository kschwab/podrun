import pytest

from podrun.podrun import _expand_volume_tilde

from conftest import FAKE_UNAME, FAKE_USER_HOME


class TestExpandVolumeTilde:
    @pytest.mark.parametrize(
        'input_args,expected',
        [
            (['-v=~/src:/dest'], [f'-v={FAKE_USER_HOME}/src:/dest']),
            (['-v=/src:~/dest'], [f'-v=/src:/home/{FAKE_UNAME}/dest']),
            (['-v=~/src:~/dest'], [f'-v={FAKE_USER_HOME}/src:/home/{FAKE_UNAME}/dest']),
            (['--volume=~/src:/dest'], [f'--volume={FAKE_USER_HOME}/src:/dest']),
            (['-v=~/src:/dest:ro'], [f'-v={FAKE_USER_HOME}/src:/dest:ro']),
            (['-v=~/data'], [f'-v={FAKE_USER_HOME}/data']),
            (['--name=test', '--env=FOO=bar'], ['--name=test', '--env=FOO=bar']),
            (
                ['--name=test', '-v=~/src:/dest', '--env=FOO=bar'],
                ['--name=test', f'-v={FAKE_USER_HOME}/src:/dest', '--env=FOO=bar'],
            ),
        ],
        ids=[
            'source',
            'dest',
            'both',
            'long-form',
            'ro-opts',
            'single-part',
            'non-volume-unchanged',
            'mixed',
        ],
    )
    def test_expand(self, input_args, expected):
        assert _expand_volume_tilde(input_args) == expected
