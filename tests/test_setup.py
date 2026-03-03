"""Podman configuration setup for project-local development.

Run this to generate podman storage and registry configuration::

    pytest tests/test_setup.py -v
    pytest tests/test_setup.py --registry=my-mirror.example.com -v

The generated configs live under ``.podrun-store/`` and are used
automatically by all live and devcontainer tests.  For manual use,
``source .podrun-store/activate``.
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


def test_activate_script(podman_store):
    """Verify activate script exists and uses CLI flags (not XDG_CONFIG_HOME)."""
    activate = PODRUN_STORE / 'activate'
    assert activate.exists(), f'{activate} not found'
    content = activate.read_text()
    assert 'PATH=' in content
    assert 'deactivate_podrun_store' in content
    assert 'XDG_CONFIG_HOME' not in content


def test_bin_wrappers(podman_store):
    """Verify bin/ wrapper scripts exist and contain store flags."""
    bin_dir = PODRUN_STORE / 'bin'
    for name in ('podman', 'podrun'):
        wrapper = bin_dir / name
        assert wrapper.exists(), f'{wrapper} not found'
        content = wrapper.read_text()
        assert '--root' in content
        assert '--runroot' in content
        assert '--storage-driver' in content


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
