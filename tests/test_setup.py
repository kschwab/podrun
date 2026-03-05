"""Podman configuration setup for project-local development.

Run this to generate podman storage and registry configuration::

    pytest tests/test_setup.py -v
    pytest tests/test_setup.py --registry=my-mirror.example.com -v

The generated configs live under ``.podrun-store/`` and are used
automatically by all live and devcontainer tests.
"""

import os
import pathlib

from conftest import PODRUN_STORE


def test_store_directories(podman_store):
    """Verify project-local podman storage directories exist."""
    graphroot = pathlib.Path(podman_store['root'])
    assert graphroot.exists(), f'{graphroot} not found'
    assert graphroot.is_dir()
    runroot_link = PODRUN_STORE / 'runroot'
    assert runroot_link.is_symlink(), f'{runroot_link} is not a symlink'
    runroot_target = pathlib.Path(os.readlink(str(runroot_link)))
    assert runroot_target.exists(), f'runroot target {runroot_target} not found'


def test_registry_config(podman_store, request):
    """Verify registry mirror configuration when --registry is provided."""
    registries = PODRUN_STORE / 'registries.conf'
    registry = request.config.getoption('--registry', default=None) or os.environ.get(
        'PODRUN_TEST_REGISTRY', ''
    )
    if registry:
        assert registries.exists(), f'{registries} not found'
        content = registries.read_text()
        assert registry in content
        assert 'docker.io' in content
