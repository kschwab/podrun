"""Devcontainer CLI integration tests for podrun.

These tests exercise the end-to-end ``devcontainer up --docker-path podrun``
flow, validating that podrun handles the full command sequence the
devcontainer CLI sends (buildx version, version --format, -v, ps, inspect,
pull, run, inspect, exec).

Run selectively::

    pytest tests/test_devcontainer_cli.py -v
"""

import json
import os
import shutil
import subprocess

import pytest

from conftest import PROJECT_ROOT

pytestmark = [
    pytest.mark.devcontainer,
    pytest.mark.skipif(shutil.which('podman') is None, reason='podman not available'),
    pytest.mark.skipif(
        shutil.which('devcontainer') is None, reason='devcontainer CLI not available'
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _devcontainer_up(workspace, devcontainer_bin, podrun_wrapper, podman_env, **kwargs):
    """Run ``devcontainer up`` with common flags, return CompletedProcess."""
    cmd = [
        devcontainer_bin,
        'up',
        '--docker-path',
        podrun_wrapper,
        '--workspace-folder',
        str(workspace),
        '--skip-post-create',
        '--mount-workspace-git-root=false',
    ]
    kwargs.setdefault('capture_output', True)
    kwargs.setdefault('text', True)
    kwargs.setdefault('timeout', 120)
    kwargs.setdefault('cwd', str(PROJECT_ROOT))
    return subprocess.run(cmd, env=podman_env, **kwargs)


def _devcontainer_exec(workspace, cmd_args, devcontainer_bin, podrun_wrapper, podman_env, **kwargs):
    """Run ``devcontainer exec`` with common flags plus *cmd_args*."""
    cmd = [
        devcontainer_bin,
        'exec',
        '--docker-path',
        podrun_wrapper,
        '--workspace-folder',
        str(workspace),
        '--mount-workspace-git-root=false',
    ] + cmd_args
    kwargs.setdefault('capture_output', True)
    kwargs.setdefault('text', True)
    kwargs.setdefault('timeout', 60)
    kwargs.setdefault('cwd', str(PROJECT_ROOT))
    return subprocess.run(cmd, env=podman_env, **kwargs)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def dev_workspace(tmp_path, pull_image):
    """Create a minimal devcontainer workspace with a pre-pulled image."""
    image = 'alpine:latest'
    pull_image(image)
    workspace = tmp_path / 'workspace'
    dc_dir = workspace / '.devcontainer'
    dc_dir.mkdir(parents=True)
    (dc_dir / 'devcontainer.json').write_text(json.dumps({'image': image}))
    return workspace


@pytest.fixture(autouse=True)
def _devcontainer_down(request, podman_store_flags, podman_env):
    """After each devcontainer test, remove containers with devcontainer labels."""
    yield
    if 'devcontainer' not in {m.name for m in request.node.iter_markers()}:
        return
    try:
        # Remove containers by devcontainer labels
        result = subprocess.run(
            ['podman']
            + podman_store_flags
            + ['ps', '-a', '--filter=label=devcontainer.local_folder', '--format={{.ID}}'],
            capture_output=True,
            text=True,
            timeout=30,
            env=podman_env,
        )
        for cid in result.stdout.strip().splitlines():
            cid = cid.strip()
            if cid:
                subprocess.run(
                    ['podman'] + podman_store_flags + ['rm', '-f', '-t', '0', cid],
                    capture_output=True,
                    timeout=30,
                    env=podman_env,
                )
        # Also remove any containers auto-named by podrun (e.g. alpine-latest)
        result = subprocess.run(
            ['podman'] + podman_store_flags + ['ps', '-a', '--format={{.Names}}'],
            capture_output=True,
            text=True,
            timeout=30,
            env=podman_env,
        )
        for name in result.stdout.strip().splitlines():
            name = name.strip()
            if name:
                subprocess.run(
                    ['podman'] + podman_store_flags + ['rm', '-f', '-t', '0', name],
                    capture_output=True,
                    timeout=30,
                    env=podman_env,
                )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDevcontainerUp:
    def test_basic_up(self, dev_workspace, devcontainer_bin, podrun_wrapper, podman_env):
        """devcontainer up → exit 0, JSON output with containerId."""
        result = _devcontainer_up(
            dev_workspace,
            devcontainer_bin,
            podrun_wrapper,
            podman_env,
        )
        assert result.returncode == 0, (
            f'devcontainer up failed:\nstdout: {result.stdout}\nstderr: {result.stderr}'
        )
        output = json.loads(result.stdout)
        assert 'containerId' in output
        assert output['containerId']

    def test_up_idempotent(self, dev_workspace, devcontainer_bin, podrun_wrapper, podman_env):
        """Second devcontainer up reuses the existing container."""
        r1 = _devcontainer_up(
            dev_workspace,
            devcontainer_bin,
            podrun_wrapper,
            podman_env,
        )
        assert r1.returncode == 0, f'first up failed:\nstdout: {r1.stdout}\nstderr: {r1.stderr}'
        cid1 = json.loads(r1.stdout)['containerId']

        r2 = _devcontainer_up(
            dev_workspace,
            devcontainer_bin,
            podrun_wrapper,
            podman_env,
        )
        assert r2.returncode == 0, f'second up failed:\nstdout: {r2.stdout}\nstderr: {r2.stderr}'
        cid2 = json.loads(r2.stdout)['containerId']
        assert cid1 == cid2


class TestDevcontainerExec:
    def test_exec_echo(self, dev_workspace, devcontainer_bin, podrun_wrapper, podman_env):
        """devcontainer exec echo hello → output contains 'hello'."""
        up = _devcontainer_up(
            dev_workspace,
            devcontainer_bin,
            podrun_wrapper,
            podman_env,
        )
        assert up.returncode == 0, (
            f'devcontainer up failed:\nstdout: {up.stdout}\nstderr: {up.stderr}'
        )

        result = _devcontainer_exec(
            dev_workspace,
            ['echo', 'hello'],
            devcontainer_bin,
            podrun_wrapper,
            podman_env,
        )
        assert 'hello' in result.stdout

    def test_exec_exit_code(self, dev_workspace, devcontainer_bin, podrun_wrapper, podman_env):
        """devcontainer exec false → non-zero exit."""
        up = _devcontainer_up(
            dev_workspace,
            devcontainer_bin,
            podrun_wrapper,
            podman_env,
        )
        assert up.returncode == 0, (
            f'devcontainer up failed:\nstdout: {up.stdout}\nstderr: {up.stderr}'
        )

        result = _devcontainer_exec(
            dev_workspace,
            ['false'],
            devcontainer_bin,
            podrun_wrapper,
            podman_env,
        )
        assert result.returncode != 0


class TestDevcontainerWithOverlays:
    def test_up_with_user_overlay(
        self, tmp_path, pull_image, has_userns, devcontainer_bin, podrun_wrapper, podman_env
    ):
        """devcontainer up with userOverlay → exec verifies user identity."""
        if not has_userns:
            pytest.skip('userns=keep-id not supported (no subuid/subgid)')
        image = 'alpine:latest'
        pull_image(image)
        workspace = tmp_path / 'workspace'
        dc_dir = workspace / '.devcontainer'
        dc_dir.mkdir(parents=True)
        dc_json = {
            'image': image,
            'customizations': {
                'podrun': {
                    'userOverlay': True,
                },
            },
        }
        (dc_dir / 'devcontainer.json').write_text(json.dumps(dc_json))

        up = _devcontainer_up(
            workspace,
            devcontainer_bin,
            podrun_wrapper,
            podman_env,
        )
        assert up.returncode == 0, (
            f'devcontainer up failed:\nstdout: {up.stdout}\nstderr: {up.stderr}'
        )

        result = _devcontainer_exec(
            workspace,
            ['id', '-u'],
            devcontainer_bin,
            podrun_wrapper,
            podman_env,
        )
        # With user overlay, the UID inside should match the host UID
        host_uid = str(os.getuid())
        assert host_uid in result.stdout.strip()
