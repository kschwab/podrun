# Minimal conftest for podrun2 tests — fully independent from tests/conftest.py.

import pytest

from podrun.podrun2 import load_podman_flags


@pytest.fixture(autouse=True, scope='session')
def _require_podman_flags():
    """Skip the entire test session if podman flags cannot be loaded.

    This succeeds when either:
    - A local (non-remote) podman binary is available, OR
    - A podrun-started container has the cache file copy-mounted in
    """
    try:
        load_podman_flags()
    except SystemExit:
        pytest.skip('podman flags unavailable (remote client without cache)')
