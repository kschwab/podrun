#!/usr/bin/env python3
# Copyright (c) 2026, Kyle Schwab
# All rights reserved.
#
# This source code is licensed under the MIT license found at
# https://github.com/kschwab/podrun/blob/main/LICENSE.md
"""
podrun
######

A podman run superset with host identity overlays.
"""

# To install latest version of podrun (script only):
# wget -nv https://raw.githubusercontent.com/kschwab/podrun/main/podrun/podrun.py -O podrun && chmod a+x podrun

# To install specific version of podrun (script only):
# wget -nv https://raw.githubusercontent.com/kschwab/podrun/<VERSION>/podrun/podrun.py -O podrun && chmod a+x podrun

# SemVer 2.0.0 (https://github.com/semver/semver/blob/master/semver.md)
# Given a version number MAJOR.MINOR.PATCH, increment the:
#  1. MAJOR version when you make incompatible API changes
#  2. MINOR version when you add functionality in a backwards compatible manner
#  3. PATCH version when you make backwards compatible bug fixes
# Additional labels for pre-release and build metadata are available as extensions to the MAJOR.MINOR.PATCH format.
__version__ = '1.0.0'
__title__ = 'podrun'
__uri__ = 'https://github.com/kschwab/podrun'
__author__ = 'Kyle Schwab'
__summary__ = 'A podman run superset with host identity overlays.'
__doc__ = __summary__
__copyright__ = 'Copyright (c) 2026, Kyle Schwab'
__license__ = (
    __copyright__
    + """
All rights reserved.

This source code is licensed under the MIT license found at
https://github.com/kschwab/podrun/blob/main/LICENSE.md"""
)

import argparse
import contextlib
import dataclasses
import getpass
import hashlib
import io
import json
import os
import pathlib
import platform
import re
import runpy
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import traceback
from typing import List, Optional, Set, Tuple

_IS_WINDOWS = sys.platform == 'win32'

if not _IS_WINDOWS:
    import pwd

# ---------------------------------------------------------------------------
# Identity and path constants
# ---------------------------------------------------------------------------

if _IS_WINDOWS:
    UID = int(os.environ.get('PODRUN_UID', '1000'))
    GID = int(os.environ.get('PODRUN_GID', '1000'))
    UNAME = getpass.getuser()
    USER_HOME = os.path.expanduser('~')
    PODRUN_TMP = os.path.join(tempfile.gettempdir(), f'podrun-{UNAME}', 'podrun')
else:
    UID = os.getuid()
    GID = os.getgid()
    UNAME = pwd.getpwuid(UID).pw_name
    USER_HOME = pwd.getpwuid(UID).pw_dir
    PODRUN_TMP = os.path.join(os.environ.get('XDG_RUNTIME_DIR', f'/tmp/podrun-{UID}'), 'podrun')
PODRUN_RC_PATH = '/.podrun/rc.sh'
PODRUN_ENTRYPOINT_PATH = '/.podrun/run-entrypoint.sh'
PODRUN_EXEC_ENTRY_PATH = '/.podrun/exec-entrypoint.sh'
PODRUN_READY_PATH = '/.podrun/READY'
PODRUN_SOCKET_PATH = '/.podrun/podman/podman.sock'
PODRUN_CONTAINER_HOST = f'unix://{PODRUN_SOCKET_PATH}'
PODRUN_HOST_TMP_MOUNT = '/.podrun/host-tmp'
BOOTSTRAP_CAPS = ['CAP_DAC_OVERRIDE', 'CAP_CHOWN', 'CAP_FOWNER', 'CAP_SETPCAP']

_NFS_REMEDIATE_DEFAULT_BASE = '/opt/podman-local-storage'
_NETWORK_FS_TYPES = frozenset(
    {
        'nfs',
        'nfs4',
        'cifs',
        'smb',
        'smbfs',
        'afs',
        'gfs',
        'gfs2',
        'gpfs',
        'lustre',
        'panfs',
        'glusterfs',
        'ceph',
    }
)

# ---------------------------------------------------------------------------
# PODRUN_* environment variable names
#
# Host-read: read by podrun on the host to configure its own behavior.
# Container-exported (always): set in every podrun container via _env_args().
# Container-exported (on-demand): set only when the relevant overlay/option
#   is active.
# Exec-session: set on ``podman exec`` via build_podman_exec_args().
# ---------------------------------------------------------------------------

# Host-read
ENV_PODRUN_PODMAN_PATH = 'PODRUN_PODMAN_PATH'  # explicit podman binary override

# Container-exported (always)
ENV_PODRUN_CONTAINER = 'PODRUN_CONTAINER'  # "inside a podrun container" marker
ENV_PODRUN_OVERLAYS = 'PODRUN_OVERLAYS'  # comma-separated overlay tokens

# Container-exported (on-demand)
ENV_PODRUN_WORKDIR = 'PODRUN_WORKDIR'  # workspace folder (host overlay)
ENV_PODRUN_SHELL = 'PODRUN_SHELL'  # shell override
ENV_PODRUN_LOGIN = 'PODRUN_LOGIN'  # login shell flag (1/0)
ENV_PODRUN_IMG = 'PODRUN_IMG'  # full image reference
ENV_PODRUN_IMG_NAME = 'PODRUN_IMG_NAME'  # image name component
ENV_PODRUN_IMG_REPO = 'PODRUN_IMG_REPO'  # image repo component
ENV_PODRUN_IMG_TAG = 'PODRUN_IMG_TAG'  # image tag component
ENV_PODRUN_ALT_ENTRYPOINT = 'PODRUN_ALT_ENTRYPOINT'  # user --entrypoint override
ENV_PODRUN_HOST_TMP = 'PODRUN_HOST_TMP'  # host-visible PODRUN_TMP for nested-remote

# Container-exported and config-script exported (on-demand)
ENV_PODRUN_PODMAN_REMOTE = 'PODRUN_PODMAN_REMOTE'  # force podman-remote resolution

# Container-exported and config-script exported (on-demand) and Host-read
ENV_PODRUN_DEVCONTAINER_CLI = 'PODRUN_DEVCONTAINER_CLI'  # invoked by devcontainer CLI

# Exec-session (set on ``podman exec``, also read inside entrypoint scripts)
ENV_PODRUN_STTY_INIT = 'PODRUN_STTY_INIT'  # terminal size for exec attach

# ns-key → PODRUN_OVERLAYS token mapping for _env_args().
_OVERLAY_FIELDS = [
    ('run.user_overlay', 'user'),
    ('run.host_overlay', 'host'),
    ('run.interactive_overlay', 'interactive'),
    ('run.dot_files_overlay', 'dotfiles'),
    ('run.session', 'session'),
    ('run.adhoc', 'adhoc'),
]

# Default dotfile volume mounts for --dot-files-overlay.
# :ro items are read-only bind mounts; :0 items are writable copies
# (resolved by _resolve_overlay_mounts via entrypoint copy-staging).
_DOTFILES = [
    # Mount dot files
    '-v=~/.emacs:~/.emacs:ro,z',
    '-v=~/.emacs.d:~/.emacs.d:ro,z',
    '-v=~/.vimrc:~/.vimrc:ro,z',
    # Copy dot files (chmod applied after copy — see _DOTFILES_CHMOD)
    '-v=~/.ssh:~/.ssh:0',
    '-v=~/.gitconfig:~/.gitconfig:0',
]

# Explicit permissions for copy-mode dotfiles.  Written as .podrun_chmod in
# the staging directory; the entrypoint applies ``chmod [-R]`` after copying.
# Keys are container paths (after tilde expansion to /home/{UNAME}).
_DOTFILES_CHMOD: dict = {
    f'/home/{UNAME}/.ssh': '700',
    f'/home/{UNAME}/.gitconfig': '600',
}

# ---------------------------------------------------------------------------
# CLI flag constants
# ---------------------------------------------------------------------------

# Podrun root flags that overlap with podman global flags and are handled
# by the root parser directly (skip registering as passthrough).
_PODRUN_HANDLED_ROOT_FLAGS = frozenset({'--version', '-v'})

# Podrun run flags that overlap with podman run value flags and are handled
# by the run parser directly (skip registering as passthrough).
_PODRUN_HANDLED_RUN_FLAGS = frozenset({'--name', '--label', '-l'})

# Docker-compat aliases that podman accepts but omits from ``podman --help``.
# These are registered as passthrough subparsers so argparse doesn't reject them.
# See: https://docs.podman.io/en/latest/markdown/podman-buildx.1.html
_DOCKER_COMPAT_SUBCOMMANDS = frozenset({'buildx'})


# ---------------------------------------------------------------------------
# PodmanFlags — scraped flag/subcommand data
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class PodmanFlags:
    global_value_flags: frozenset
    global_boolean_flags: frozenset
    subcommands: frozenset
    run_value_flags: frozenset
    run_boolean_flags: frozenset
    bool_short_to_long: dict = dataclasses.field(default_factory=dict)  # e.g. {'-d': '--detach'}


# In-memory cache keyed by podman_path.
_loaded_flags: dict = {}


def _flags_cache_dir():
    """Return the platform-appropriate podrun cache directory.

    Linux: ``$XDG_CACHE_HOME/podrun`` or ``~/.cache/podrun``
    Windows: ``%LOCALAPPDATA%/podrun`` or ``~/podrun``
    """
    if _IS_WINDOWS:
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    else:
        base = os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache')
    return os.path.join(base, 'podrun')


def _flags_cache_path(podman_path='podman'):
    """Return the disk cache path for scraped podman flags.

    Cache key is derived from ``os.stat()`` on the binary — catches upgrades
    without spawning a subprocess (~1 µs vs ~800 ms on Windows).  Falls back
    to ``'unknown'`` if stat fails.

    Uses ``podman-remote`` in the label when operating in remote mode
    (binary is ``podman-remote`` or ``CONTAINER_HOST`` is set), so the
    cache reflects the actual flag set rather than the binary name.
    """
    label = 'podman-remote' if _is_remote(podman_path) else 'podman'
    resolved = shutil.which(podman_path) if not os.path.isabs(podman_path) else podman_path
    try:
        st = os.stat(resolved or podman_path)
        key = f'{label}-{st.st_mtime_ns}-{st.st_size}'
    except OSError:
        key = f'{label}-unknown'
    return os.path.join(_flags_cache_dir(), f'{key}.json')


def _scrape_all_flags(podman_path):
    """Scrape global and run flags from podman --help and return a PodmanFlags."""
    global_result = _scrape_podman_help(podman_path)
    if global_result is None:
        raise RuntimeError(f'Failed to scrape {podman_path} --help')

    global_value, global_bool, subcmds, global_stl = global_result
    # Filter out 'help' — podman lists it but we don't register it as a subparser.
    subcmds.discard('help')

    run_result = _scrape_podman_help(podman_path, subcmd='run')
    if run_result is None:
        raise RuntimeError(f'Failed to scrape {podman_path} run --help')

    run_value, run_bool, _, run_stl = run_result

    # Merge short→long mappings from both global and run scopes.
    bool_short_to_long = {**global_stl, **run_stl}

    return PodmanFlags(
        global_value_flags=frozenset(global_value),
        global_boolean_flags=frozenset(global_bool),
        subcommands=frozenset(subcmds),
        run_value_flags=frozenset(run_value),
        run_boolean_flags=frozenset(run_bool),
        bool_short_to_long=bool_short_to_long,
    )


def _read_flags_cache(path):
    """Read a PodmanFlags JSON cache file, return PodmanFlags or None."""
    try:
        with open(path) as f:
            data = json.load(f)
        return PodmanFlags(
            global_value_flags=frozenset(data['global_value_flags']),
            global_boolean_flags=frozenset(data['global_boolean_flags']),
            subcommands=frozenset(data['subcommands']),
            run_value_flags=frozenset(data['run_value_flags']),
            run_boolean_flags=frozenset(data['run_boolean_flags']),
            bool_short_to_long=data.get('bool_short_to_long', {}),
        )
    except (OSError, KeyError, json.JSONDecodeError):
        return None


def _write_flags_cache(path, flags):
    """Write a PodmanFlags to a JSON cache file.

    Silently ignores write failures (e.g. read-only cache dir inside a
    container).  The in-memory cache (``_loaded_flags``) still serves the
    current process; re-scraping on next invocation is fast and acceptable.
    """
    data = {
        'global_value_flags': sorted(flags.global_value_flags),
        'global_boolean_flags': sorted(flags.global_boolean_flags),
        'subcommands': sorted(flags.subcommands),
        'run_value_flags': sorted(flags.run_value_flags),
        'run_boolean_flags': sorted(flags.run_boolean_flags),
        'bool_short_to_long': flags.bool_short_to_long,
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


def _clean_stale_cache(current_cache_path):
    """Remove old .json cache files for the same binary label.

    Called after writing a new cache file so that stale entries (from previous
    stat keys) don't accumulate.  Only removes files matching the same label
    (``podman`` or ``podman-remote``), leaving the other binary's cache intact.
    Silently ignores errors.
    """
    cache_dir = os.path.dirname(current_cache_path)
    current_basename = os.path.basename(current_cache_path)
    is_remote = current_basename.startswith('podman-remote-')
    try:
        for f in os.listdir(cache_dir):
            if not f.endswith('.json') or f == current_basename:
                continue
            # Only clean files with the same label (remote vs non-remote).
            if f.startswith('podman-remote-') == is_remote:
                try:
                    os.remove(os.path.join(cache_dir, f))
                except OSError:
                    pass
    except OSError:
        pass


def load_podman_flags(podman_path=None):
    """Load podman flags via in-memory cache, disk cache, or live scrape.

    Resolution chain:
    1. In-memory cache hit → return immediately
    2. Disk cache (stat-based key) → read, store in memory, return
    3. Live scrape from ``podman_path --help`` → write cache, return

    The cache key is derived from ``os.stat()`` on the binary, so no
    subprocess is needed on the warm-cache path.

    When *podman_path* is ``None``, resolves via ``_default_podman_path()``.
    Scraping works for both ``podman`` and ``podman-remote``.  The latter
    returns fewer global flags, which is correct — the cache-aware flag
    filter (``_filter_global_args``) uses this to silently drop unsupported
    flags when remote.
    """
    if podman_path is None:
        podman_path = _default_podman_path() or 'podman'
    if podman_path in _loaded_flags:
        return _loaded_flags[podman_path]

    # Stat-based cache lookup — no subprocess call on warm cache.
    cache_path = _flags_cache_path(podman_path)
    flags = _read_flags_cache(cache_path)
    if flags is not None:
        _loaded_flags[podman_path] = flags
        return flags

    flags = _scrape_all_flags(podman_path)
    _write_flags_cache(cache_path, flags)
    # Clean stale cache files from previous versions/stat keys.
    _clean_stale_cache(cache_path)
    _loaded_flags[podman_path] = flags
    return flags


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def _shell_quote(s: str) -> str:
    """Quote a string for the platform's shell.

    On POSIX, delegates to ``shlex.quote`` (single-quote wrapping).
    On Windows, wraps in double quotes with internal double-quotes escaped
    (``cmd.exe`` convention).
    """
    if not _IS_WINDOWS:
        return shlex.quote(s)
    if not s:
        return '""'
    # If it contains no special characters, return as-is.
    if not any(c in s for c in ' \t"&|<>^%'):
        return s
    return '"' + s.replace('"', '\\"') + '"'


def run_os_cmd(cmd: str, env: Optional[dict] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=True,
        universal_newlines=True,
        env=env,
    )


def _exec_or_subprocess(cmd: List[str], env: dict) -> None:
    """Replace the current process with *cmd*, or use subprocess on Windows.

    On POSIX, calls ``os.execvpe`` (never returns).
    On Windows, ``os.execvpe`` is unreliable — use ``subprocess.run`` instead.
    """
    if _IS_WINDOWS:
        result = subprocess.run(cmd, env=env)
        sys.exit(result.returncode)
    else:
        os.execvpe(cmd[0], cmd, env)


def _clean_dir(label: str, directory: str) -> Tuple[List[str], List[str]]:
    """Remove *directory* if it exists and is non-empty.

    Returns ``(removed, failed)`` lists of human-readable descriptions.
    """
    removed: List[str] = []
    failed: List[str] = []
    if os.path.isdir(directory) and os.listdir(directory):
        try:
            shutil.rmtree(directory)
        except OSError as e:
            print(f'Error removing {directory}: {e}', file=sys.stderr)
        if not os.path.isdir(directory):
            removed.append(f'{label}: {directory}')
        else:
            failed.append(f'{label}: {directory}')
    return removed, failed


def _collect_container_staging(container: dict, protected: set) -> None:
    """Add PODRUN_TMP-resident mount sources from *container* to *protected*.

    For each mount whose source lives under PODRUN_TMP, the *top-level*
    entry (what ``os.listdir(PODRUN_TMP)`` would show) is added.  This
    correctly protects nested structures like ``copy-staging/{sha12}``.

    Also protects the config sidecar file for named containers that have
    staging mounts.
    """
    tmp_prefix = PODRUN_TMP + os.sep
    name = (container.get('Name') or '').lstrip('/')
    mounts = container.get('Mounts') or []
    has_staging = False
    for mount in mounts:
        src = mount.get('Source', '')
        if src.startswith(tmp_prefix):
            # First component relative to PODRUN_TMP — handles both flat
            # files (entrypoint_abc.sh) and nested dirs (copy-staging/sha).
            top_level = os.path.relpath(src, PODRUN_TMP).split(os.sep)[0]
            if top_level:
                protected.add(top_level)
                has_staging = True
    if name and has_staging:
        sidecar_name = f'config_{name}.json'
        if os.path.isfile(os.path.join(PODRUN_TMP, sidecar_name)):
            protected.add(sidecar_name)


def _protected_staging_files() -> set:
    """Return basenames in PODRUN_TMP that are mounted into existing containers.

    Inspects all containers (running or stopped) and collects mount sources
    under PODRUN_TMP.  Also protects config sidecars for named containers.
    Returns an empty set if podman is not available or no containers exist.
    """
    protected: set = set()
    podman = shutil.which('podman') or shutil.which('podman-remote')
    if not podman or not os.path.isdir(PODRUN_TMP):
        return protected

    r = run_os_cmd(f'{_shell_quote(podman)} ps -aq')
    if r.returncode != 0 or not r.stdout.strip():
        return protected

    ids = [cid.strip() for cid in r.stdout.strip().split('\n') if cid.strip()]
    if not ids:
        return protected

    ids_str = ' '.join(_shell_quote(cid) for cid in ids)
    r = run_os_cmd(f'{_shell_quote(podman)} inspect {ids_str}')
    if r.returncode != 0:
        return protected
    try:
        containers = json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return protected

    for container in containers:
        _collect_container_staging(container, protected)

    return protected


def _clean_staging() -> Tuple[List[str], List[str]]:
    """Remove entrypoint scripts and staging files from PODRUN_TMP.

    Preserves files mounted into existing containers (running or stopped)
    by inspecting their mount sources.  Falls back to removing everything
    if podman is not available or no containers reference PODRUN_TMP.
    """
    if not os.path.isdir(PODRUN_TMP) or not os.listdir(PODRUN_TMP):
        return [], []

    protected = _protected_staging_files()
    if not protected:
        return _clean_dir('Entrypoint scripts and staging', PODRUN_TMP)

    # Selective removal: delete everything except protected files.
    removed_items: List[str] = []
    preserved_items: List[str] = []
    failed_items: List[str] = []
    for entry in os.listdir(PODRUN_TMP):
        if entry in protected:
            print(f'  preserved: {entry}', file=sys.stderr)
            preserved_items.append(entry)
            continue
        path = os.path.join(PODRUN_TMP, entry)
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
            print(f'  removed: {entry}', file=sys.stderr)
            removed_items.append(entry)
        except OSError as e:
            print(f'  error: {entry}: {e}', file=sys.stderr)
            failed_items.append(entry)

    removed: List[str] = []
    failed: List[str] = []
    if removed_items or preserved_items:
        removed.append(
            f'Staging ({len(removed_items)} removed, '
            f'{len(preserved_items)} preserved): {PODRUN_TMP}'
        )
    if failed_items:
        failed.append(f'Staging files ({len(failed_items)} items): {PODRUN_TMP}')
    return removed, failed


def _clean_cache() -> Tuple[List[str], List[str]]:
    """Remove podman flags cache directory."""
    return _clean_dir('Podman flags cache', _flags_cache_dir())


def _clean_stores() -> Tuple[List[str], List[str]]:  # noqa: C901
    """Stop idle store services and remove their runtime dirs.

    Skips entries with active containers (running or stopped).
    Returns ``(removed, failed)`` lists of human-readable descriptions.
    """
    removed: List[str] = []
    removed_entries: List[str] = []
    failed: List[str] = []
    if os.path.isdir(_PODRUN_STORES_DIR):
        all_removed = True
        had_entries = False
        for name in os.listdir(_PODRUN_STORES_DIR):
            entry = os.path.join(_PODRUN_STORES_DIR, name)
            if not os.path.isdir(entry):
                continue
            had_entries = True
            sock = os.path.join(entry, 'podman.sock')
            pid_path = os.path.join(entry, 'podman.pid')
            # Check if any containers exist (running or stopped).
            # Stopped containers still depend on the runroot for restart.
            # Primary check: overlay-containers/ subdirs containing
            # userdata/config.json on disk (works even when the store
            # service is down).  Bare subdirs without config.json are
            # stale metadata left after container removal and are ignored.
            # Fallback: query via socket if the service is up.
            containers_dir = os.path.join(entry, 'overlay-containers')
            has_containers = False
            if os.path.isdir(containers_dir):
                for cdir in os.listdir(containers_dir):
                    cfg = os.path.join(containers_dir, cdir, 'userdata', 'config.json')
                    if os.path.isfile(cfg):
                        has_containers = True
                        break
            if not has_containers and os.path.exists(sock):
                try:
                    r = subprocess.run(
                        ['podman', '--url', f'unix://{sock}', 'ps', '-qa'],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    has_containers = r.returncode == 0 and r.stdout.strip() != ''
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if has_containers:
                all_removed = False
                print(f'Skipped: Store service entry (containers exist): {entry}', file=sys.stderr)
                continue
            # No active containers — stop service if alive, then remove.
            if os.path.isfile(pid_path):
                try:
                    pid = int(open(pid_path).read().strip())  # noqa: SIM115
                    os.kill(pid, signal.SIGTERM)
                    print(f'Stopped: Store service (PID {pid}): {entry}', file=sys.stderr)
                except (ValueError, OSError):
                    pass
            try:
                shutil.rmtree(entry, ignore_errors=False)
            except PermissionError:
                # Runroot dirs contain UID-mapped files from podman's user
                # namespace — need `podman unshare` to remove them.
                try:
                    subprocess.run(
                        ['podman', 'unshare', 'rm', '-rf', entry],
                        capture_output=True,
                        timeout=30,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
                # Retry: unshare may have removed UID-mapped files but left
                # regular files/dirs behind.
                if os.path.isdir(entry):
                    try:
                        shutil.rmtree(entry)
                    except OSError as e:
                        print(f'Error removing {entry}: {e}', file=sys.stderr)
            except OSError:
                # Race: file disappeared between listing and unlink (e.g.
                # store service removed its socket during shutdown).  Retry.
                try:
                    shutil.rmtree(entry)
                except OSError as e:
                    print(f'Error removing {entry}: {e}', file=sys.stderr)
            if os.path.isdir(entry):
                all_removed = False
                failed.append(f'Store service entry: {entry}')
            else:
                removed_entries.append(entry)
        if all_removed:
            # All entries gone (or empty) — remove the parent dir.
            try:
                shutil.rmtree(_PODRUN_STORES_DIR)
            except OSError:
                pass
            if had_entries and not os.path.isdir(_PODRUN_STORES_DIR):
                removed.append(f'Store service runtime (sockets, PIDs): {_PODRUN_STORES_DIR}')
        elif removed_entries:
            removed.append(f'Store service entries: {", ".join(removed_entries)}')
    return removed, failed


def _report_cleanup(removed: List[str], failed: List[str]) -> None:
    """Print cleanup results to stderr."""
    for item in removed:
        print(f'Removed {item}', file=sys.stderr)
    for item in failed:
        print(f'Failed to remove {item}', file=sys.stderr)
    if removed and not failed:
        print('All cleaned up.', file=sys.stderr)
    elif not removed and not failed:
        print('Nothing to clean.', file=sys.stderr)


_CLEANUP_MODES = {'all', 'staging', 'cache', 'stores'}

_CLEANUP_DISPATCH = {
    'staging': _clean_staging,
    'cache': _clean_cache,
    'stores': _clean_stores,
}


def _handle_cleanup(modes: List[str]) -> None:
    """Dispatch cleanup by *modes* and report results."""
    all_removed: List[str] = []
    all_failed: List[str] = []
    if 'all' in modes:
        for fn in (_clean_stores, _clean_staging, _clean_cache):
            r, f = fn()
            all_removed.extend(r)
            all_failed.extend(f)
    else:
        seen: Set[str] = set()
        for mode in modes:
            if mode in seen:
                continue
            seen.add(mode)
            r, f = _CLEANUP_DISPATCH[mode]()
            all_removed.extend(r)
            all_failed.extend(f)
    _report_cleanup(all_removed, all_failed)


def _parse_cleanup_modes(raw: List[str]) -> Optional[List[str]]:
    """Pre-parse *raw* argv for ``--cleanup`` / ``--__cleanup__`` flags.

    Returns a list of mode strings, or ``None`` when no cleanup flag is
    present.  Prints an error and exits on invalid modes.
    """
    modes: List[str] = []
    i = 0
    while i < len(raw):
        arg = raw[i]
        if arg == '--__cleanup__':
            modes.append('all')
        elif arg == '--cleanup' and i + 1 < len(raw):
            modes.append(raw[i + 1])
            i += 1
        elif arg.startswith('--cleanup='):
            modes.append(arg.split('=', 1)[1])
        i += 1
    if not modes:
        return None
    for m in modes:
        if m not in _CLEANUP_MODES:
            print(
                f'Error: invalid --cleanup mode {m!r}. '
                f'Choose from: {", ".join(sorted(_CLEANUP_MODES))}',
                file=sys.stderr,
            )
            sys.exit(2)
    return modes


def _default_podman_path():
    """Resolve the default podman binary.

    Resolution order:

    1. ``PODRUN_PODMAN_PATH`` env var — highest priority, checked before any
       parsing or flag scraping.  Follows the standard ``CC``/``EDITOR``
       convention for tool-path overrides.
    2. ``PODRUN_PODMAN_REMOTE`` env var set → require ``podman-remote``.
       Falls back to ``podman`` only when ``CONTAINER_HOST`` is also set
       (which makes ``podman`` act as a remote client).  Hard error if
       ``podman-remote`` is not found and fallback is not possible.
    3. ``podman`` — preferred when available.
    4. ``podman-remote`` — fallback when ``podman`` is not found (fixes
       ``--help`` inside containers with only ``podman-remote`` installed).
    """
    env_path = os.environ.get(ENV_PODRUN_PODMAN_PATH)
    if env_path:
        resolved = shutil.which(env_path)
        if not resolved:
            print(f'Error: {ENV_PODRUN_PODMAN_PATH}={env_path!r} not found.', file=sys.stderr)
            sys.exit(1)
        return resolved
    if os.environ.get(ENV_PODRUN_PODMAN_REMOTE):
        remote = shutil.which('podman-remote')
        if remote:
            return remote
        # podman is valid as fallback only when CONTAINER_HOST makes it
        # act as a remote client.
        if os.environ.get('CONTAINER_HOST'):
            podman = shutil.which('podman')
            if podman:
                return podman
        print(
            f'Error: {ENV_PODRUN_PODMAN_REMOTE} is set but podman and podman-remote were not found.',
            file=sys.stderr,
        )
        sys.exit(1)
    found = shutil.which('podman')
    if found:
        return found
    return shutil.which('podman-remote')


def _is_remote(podman_path: str) -> bool:
    """Return True when operating as a remote podman client.

    True on Windows (podman is always remote via podman machine), when the
    binary is ``podman-remote``, or when ``CONTAINER_HOST`` is set (which
    causes even the full ``podman`` binary to act as a remote client with a
    reduced flag set).
    """
    if _IS_WINDOWS:
        return True
    return os.path.basename(podman_path) == 'podman-remote' or bool(
        os.environ.get('CONTAINER_HOST')
    )


def _is_network_fs(path: str) -> bool:  # noqa: C901
    """Return True when *path* resides on a network filesystem.

    Walks up to the nearest existing ancestor, then checks filesystem type
    via ``stat -f -c '%T'`` (preferred) and ``df -T`` (fallback).  Returns
    True if the detected type is in :data:`_NETWORK_FS_TYPES` or contains
    ``'nfs'``.  Returns False gracefully when the commands are unavailable
    (e.g. on Windows).
    """
    check = pathlib.Path(path)
    while not check.exists():
        parent = check.parent
        if parent == check:
            return False
        check = parent
    check_str = str(check)
    # Primary: stat -f -c '%T'
    try:
        result = subprocess.run(
            ['stat', '-f', '-c', '%T', check_str],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            fs_type = result.stdout.strip().lower()
            if fs_type in _NETWORK_FS_TYPES or 'nfs' in fs_type:
                return True
            return False
    except FileNotFoundError:
        pass
    # Fallback: df -T
    try:
        result = subprocess.run(
            ['df', '-T', check_str],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 2:
                    fs_type = parts[1].lower()
                    if fs_type in _NETWORK_FS_TYPES or 'nfs' in fs_type:
                        return True
    except FileNotFoundError:
        pass
    return False


def _warn_missing_subids():
    """Print a note if the current user lacks subuid/subgid ranges."""
    try:
        missing = []
        for path in ('/etc/subuid', '/etc/subgid'):
            try:
                with open(path) as f:
                    if UNAME not in f.read():
                        missing.append(path)
            except FileNotFoundError:
                missing.append(path)
        if missing:
            print(f'\nNote: {UNAME} not found in {" or ".join(missing)}.', file=sys.stderr)
            print('  Podman will show rootless warnings and --userns=keep-id', file=sys.stderr)
            print('  (used by --user-overlay) will not work. To fix:', file=sys.stderr)
            print(
                f'    sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 {UNAME}',
                file=sys.stderr,
            )
    except Exception:
        pass


def _config_split(text: str) -> List[str]:
    """Split config script output into tokens.

    Like ``shlex.split()`` but backslashes are not treated as escape
    characters.  Config script output is program data, not shell source
    code — quotes delimit tokens, but ``\\`` is a literal character (not
    an escape prefix).  This matches how devcontainer.json treats values
    after JSON parsing and avoids mangling Windows paths like ``C:\\Users``.
    """
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.escape = ''
    return list(lexer)


def _run_script_in_process(path: str, extra: dict) -> str:
    """Run a single Python config script in-process and return its stdout.

    Sets *extra* env vars for the duration and restores them afterward.
    Calls ``sys.exit(1)`` on script failure.
    """
    saved = os.environ.copy()
    try:
        os.environ.update(extra)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            runpy.run_path(path, run_name='__main__')
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (1 if exc.code else 0)
        if code != 0:
            print(f'Error: --config-script {path} failed (exit {code})', file=sys.stderr)
            sys.exit(1)
    except Exception:
        traceback.print_exc()
        print(f'Error: --config-script {path} raised an exception', file=sys.stderr)
        sys.exit(1)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return buf.getvalue()


def run_config_scripts(script_paths: List[str], ctx: Optional['PodrunContext'] = None) -> List[str]:
    """Execute scripts in-process left-to-right, return concatenated parsed tokens.

    Fatal (sys.exit(1)) on non-zero exit or unhandled exception.

    When *ctx* is provided, scripts receive on-demand env vars so they
    can branch on context:

    - ``PODRUN_DEVCONTAINER_CLI=1`` when ``ctx.dc_from_cli`` is set
    - ``PODRUN_PODMAN_REMOTE=1`` when the resolved podman binary is remote
    """
    extra: dict = {}
    if ctx:
        if ctx.dc_from_cli or ctx.ns.get('internal.dc_from_cli'):
            extra[ENV_PODRUN_DEVCONTAINER_CLI] = '1'
        if _is_remote(ctx.podman_path):
            extra[ENV_PODRUN_PODMAN_REMOTE] = '1'

    tokens: List[str] = []
    for path in script_paths:
        tokens.extend(_config_split(_run_script_in_process(path, extra)))
    return tokens


def parse_config_tokens(tokens: List[str], flags=None) -> Tuple[dict, List[str]]:  # noqa: C901
    """Parse config tokens through root + run parsers.

    Returns (config_ns_dict, podman_passthrough).
    config_ns_dict has only non-None values with root.*/run.* keys.

    Config tokens don't include subcommands.  The root parser is tried
    first (for global flags like ``--store``); if it errors on a
    positional that looks like an invalid subcommand, all tokens are
    forwarded to the run parser instead.
    """
    if not tokens:
        return {}, []

    # Config scripts must not emit meta-controls that govern config resolution
    # itself — that would create circular or ambiguous resolution order.
    _FORBIDDEN = {'--devconfig', '--config-script', '--no-devconfig'}
    found = _FORBIDDEN.intersection(tokens)
    if found:
        print(
            f'Error: config-script output must not contain {", ".join(sorted(found))}',
            file=sys.stderr,
        )
        sys.exit(1)

    # Disambiguate -v: on the root parser -v is --version (boolean), but
    # podman run uses -v as --volume (value).  Config scripts commonly emit
    # -v=<host>:<ctr> or -v <host>:<ctr>.  Extract these before parsing and
    # inject them directly into passthrough output.
    volume_passthrough: List[str] = []
    remaining: List[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i].startswith('-v='):
            volume_passthrough.extend(['-v', tokens[i][3:]])
        elif tokens[i] == '-v' and i + 1 < len(tokens) and ':' in tokens[i + 1]:
            volume_passthrough.extend(['-v', tokens[i + 1]])
            i += 1
        else:
            remaining.append(tokens[i])
        i += 1
    tokens = remaining

    root = build_root_parser(flags)

    # Suppress subcommand validation — config tokens have no subcommand.
    # Remove the subcommand subparsers action so positionals don't trigger
    # "invalid choice" errors.
    saved_actions = root._subparsers._group_actions[:]  # type: ignore[union-attr]
    root._subparsers._group_actions.clear()  # type: ignore[union-attr]
    # Also remove the subparsers action from _actions to prevent positional matching
    saved_sub_actions = [a for a in root._actions if isinstance(a, argparse._SubParsersAction)]
    for a in saved_sub_actions:
        root._actions.remove(a)

    root_ns, unknowns = root.parse_known_args(tokens)
    root_dict = vars(root_ns)

    # Restore actions
    root._subparsers._group_actions.extend(saved_actions)  # type: ignore[union-attr]
    root._actions.extend(saved_sub_actions)

    # Second pass: run parser on unknowns
    run_parser = root._run_subparser  # type: ignore[attr-defined]
    run_ns, podman_passthrough = run_parser.parse_known_args(unknowns)
    run_dict = vars(run_ns)

    # Merge, keeping only explicitly-set values with root.* or run.* keys.
    # Exclude False (store_true defaults), None, and passthrough/trailing lists.
    _SKIP_KEYS = {'run.passthrough_args', 'run.trailing', 'run.print_overlays'}
    config_ns = {}
    for src in (root_dict, run_dict):
        for k, v in src.items():
            if k in _SKIP_KEYS:
                continue
            if v is None or v is False:
                continue
            if not (k.startswith('root.') or k.startswith('run.')):
                continue
            config_ns[k] = v

    # Podman passthrough = extracted volumes + unknowns + run.passthrough_args
    run_passthrough = run_dict.get('run.passthrough_args') or []
    podman_passthrough = volume_passthrough + podman_passthrough + run_passthrough

    return config_ns, podman_passthrough


# ---------------------------------------------------------------------------
# Parsing helpers and utilities
# ---------------------------------------------------------------------------


def yes_no_prompt(prompt_msg: str, answer_default: bool, is_interactive: bool) -> bool:
    """Prompt the user for a yes/no answer on stderr."""
    prompt_default = 'Y/n' if answer_default else 'N/y'
    answer_default_str = 'yes' if answer_default else 'no'
    prompt_str = f'{prompt_msg} [{prompt_default}]: '
    if is_interactive:
        sys.stderr.write(prompt_str)
        sys.stderr.flush()
        answer = input().lower() or answer_default_str
    else:
        answer = answer_default_str
        print(f'{prompt_str}{answer}', file=sys.stderr)
    while answer[:1] not in ['y', 'n']:
        print('Please answer yes or no...', file=sys.stderr)
        sys.stderr.write(prompt_str)
        sys.stderr.flush()
        answer = input().lower() or answer_default_str
    return answer[:1] == 'y'


def _split_path_colon(entry: str) -> list:
    """Split on ``:`` while preserving Windows drive letters (``C:\\``)."""
    parts = entry.split(':')
    merged: list = []
    i = 0
    while i < len(parts):
        p = parts[i]
        # Single letter followed by a part starting with \ or / is a drive letter.
        if len(p) == 1 and p.isalpha() and i + 1 < len(parts) and parts[i + 1][:1] in ('\\', '/'):
            merged.append(p + ':' + parts[i + 1])
            i += 2
        else:
            merged.append(p)
            i += 1
    return merged


def _parse_export(entry: str):
    """Parse an export entry into ``(container_path, host_path, copy_only)``.

    Accepted forms::

        container_path:host_path        — strict (rm + symlink)
        container_path:host_path:0      — copy-only (populate host dir, skip rm/symlink)
    """
    parts = _split_path_colon(entry)
    if len(parts) == 3 and parts[2] == '0':
        return parts[0], parts[1], True
    if len(parts) == 2:
        return parts[0], parts[1], False
    raise ValueError(f'Invalid export spec {entry!r}: expected SRC:DST or SRC:DST:0')


def _parse_image_ref(image: str) -> Tuple[str, str, str]:
    """Break an image reference into ``(registry, name, tag)``.

    Registry defaults to ``docker.io`` and tag defaults to ``latest``
    when not explicitly present.
    """
    _IMAGE_RE = re.compile(
        r'^((?P<registry>([^/]*[\.:]|localhost)[^/]*)/)?'
        r'/?(?P<name>[a-z0-9][^:]*):?(?P<tag>.*)'
    )
    m = _IMAGE_RE.match(image)
    if m is None:
        raise ValueError(f'Invalid image name: "{image}"')
    parts = m.groupdict()
    return (
        parts['registry'] if parts['registry'] else 'docker.io',
        parts['name'],
        parts['tag'] if parts['tag'] else 'latest',
    )


def _write_sha_file(content: str, prefix: str, suffix: str) -> str:
    """Write content to a SHA-named file and return the daemon-visible path.

    In nested-remote mode (``PODRUN_CONTAINER=1`` + ``PODRUN_HOST_TMP``),
    the file is physically written to :func:`_staging_dir` (the bind-mounted
    host directory) but the returned path uses :func:`_daemon_dir` so that
    ``-v`` args reference a path the host daemon can resolve.

    Always overwrites — the files are small and avoiding staleness
    (e.g. wrong line endings from a previous platform) is worth the
    negligible I/O cost.
    """
    content_hash = hashlib.sha256(content.encode()).hexdigest()[:12]
    filename = f'{prefix}{content_hash}{suffix}'
    write_dir = _staging_dir()
    pathlib.Path(write_dir).mkdir(parents=True, exist_ok=True)
    write_path = os.path.join(write_dir, filename)
    with open(write_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(content)
    os.chmod(write_path, 0o755)
    return os.path.join(_daemon_dir(), filename)


def _staging_dir() -> str:
    """Return the directory where podrun physically writes staging files.

    In nested-remote mode the outer podrun bind-mounts its ``PODRUN_TMP``
    at ``PODRUN_HOST_TMP_MOUNT`` inside the container.  Writing there
    makes files visible to the host daemon.  Otherwise returns ``PODRUN_TMP``.
    """
    host_tmp = os.environ.get(ENV_PODRUN_HOST_TMP)
    if host_tmp and os.environ.get(ENV_PODRUN_CONTAINER):
        return PODRUN_HOST_TMP_MOUNT
    return PODRUN_TMP


def _daemon_dir() -> str:
    """Return the base path the daemon uses to see staging files.

    In nested-remote mode this is the ``PODRUN_HOST_TMP`` env var value
    (the original host path).  Otherwise returns ``PODRUN_TMP``.
    """
    host_tmp = os.environ.get(ENV_PODRUN_HOST_TMP)
    if host_tmp and os.environ.get(ENV_PODRUN_CONTAINER):
        return host_tmp
    return PODRUN_TMP


def _write_mount_manifest(mount_map: dict, copy_staging: Optional[list] = None) -> str:
    """Write a mount manifest recording daemon-visible sources for each mount.

    *mount_map* is a ``{container_dest: host_source}`` dict built by
    :func:`_process_volume_args`.

    *copy_staging* is an optional list of ``(host_path, container_path)``
    tuples recorded in a separate ``copy_staging`` section.

    Returns the path the manifest was written to.
    """
    cs: dict = {}
    if copy_staging:
        for host_path, container_path in copy_staging:
            cs[container_path] = host_path

    manifest = {'mounts': mount_map, 'copy_staging': cs}
    write_dir = _staging_dir()
    pathlib.Path(write_dir).mkdir(parents=True, exist_ok=True)
    manifest_path = os.path.join(write_dir, 'mount-manifest.json')
    with open(manifest_path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(manifest, f)
    return manifest_path


def _read_mount_manifest() -> dict:
    """Read the mount manifest written by an outer podrun.

    Returns the parsed dict or ``{"mounts": {}, "copy_staging": {}}`` if
    the manifest file is missing or unreadable.
    """
    manifest_path = os.path.join(_staging_dir(), 'mount-manifest.json')
    try:
        with open(manifest_path, encoding='utf-8') as f:
            data: dict = json.load(f)
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {'mounts': {}, 'copy_staging': {}}


# ---------------------------------------------------------------------------
# Config sidecar — per-container config file hashes for drift detection
# ---------------------------------------------------------------------------


def _config_sidecar_path(name: str) -> str:
    """Return the path to the config sidecar JSON for container *name*."""
    return os.path.join(_staging_dir(), f'config_{name}.json')


def _hash_file(path: str) -> Optional[str]:
    """Return the SHA-256 hex digest of a file's contents, or None if unreadable."""
    try:
        with open(path, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _write_config_sidecar(ns: dict) -> None:
    """Write the config sidecar JSON for a named container.

    Records config file hashes and entrypoint filenames so that
    attach/restart can detect config drift and clean can find orphans.
    """
    name = ns.get('run.name')
    if not name:
        return

    config_files: dict = {}
    dc_path = ns.get('internal.config_dc_path')
    if dc_path:
        h = _hash_file(dc_path)
        if h:
            config_files[dc_path] = h
    rc_path = ns.get('internal.config_rc_path')
    if rc_path:
        h = _hash_file(rc_path)
        if h:
            config_files[rc_path] = h
    for sp in ns.get('internal.config_script_paths') or []:
        abs_sp = os.path.abspath(sp)
        h = _hash_file(abs_sp)
        if h:
            config_files[abs_sp] = h

    sidecar: dict = {
        'config_files': config_files,
        'created': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
    }

    # Entrypoint references (basenames) for orphan cleanup
    for key in ('internal.entrypoint_path', 'internal.rc_path', 'internal.exec_entry_path'):
        val = ns.get(key)
        if val:
            sidecar[key.rsplit('.', 1)[1]] = os.path.basename(val)

    path = _config_sidecar_path(name)
    pathlib.Path(os.path.dirname(path)).mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(sidecar, f, indent=2)


def _read_config_sidecar(name: str) -> Optional[dict]:
    """Read the config sidecar JSON for container *name*, or None if absent."""
    path = _config_sidecar_path(name)
    try:
        with open(path, encoding='utf-8') as f:
            data: dict = json.load(f)
            return data
    except (OSError, json.JSONDecodeError):
        return None


def _detect_config_drift(ns: dict) -> List[str]:
    """Compare current config file hashes against stored sidecar.

    Returns a list of changed file paths (empty = no drift).
    """
    name = ns.get('run.name')
    if not name:
        return []

    sidecar = _read_config_sidecar(name)
    if sidecar is None:
        return []  # No sidecar → container predates this feature, skip

    stored_files = sidecar.get('config_files', {})
    changed: List[str] = []

    # Check stored files for modifications or deletions
    for path, stored_hash in stored_files.items():
        current_hash = _hash_file(path)
        if current_hash != stored_hash:
            changed.append(path)

    # Check for new config files not in sidecar
    current_paths: List[str] = []
    dc_path = ns.get('internal.config_dc_path')
    if dc_path:
        current_paths.append(dc_path)
    rc_path = ns.get('internal.config_rc_path')
    if rc_path:
        current_paths.append(rc_path)
    for sp in ns.get('internal.config_script_paths') or []:
        current_paths.append(os.path.abspath(sp))

    for cp in current_paths:
        if cp not in stored_files:
            changed.append(cp)

    return changed


def _config_drift_prompt(
    name: str, changed_files: List[str], is_interactive: bool
) -> Optional[str]:
    """Prompt user about config drift.

    Returns ``'continue'``, ``'replace'``, or ``None`` (quit).
    """
    print(
        f'Warning: Config has changed since container {name!r} was created:',
        file=sys.stderr,
    )
    for f in changed_files:
        print(f'  modified: {f}', file=sys.stderr)

    if not is_interactive:
        print('  (non-interactive: continuing with stale config)', file=sys.stderr)
        return 'continue'

    prompt_str = 'Continue with stale config? [C]ontinue / [R]eplace / [Q]uit: '
    while True:
        sys.stderr.write(prompt_str)
        sys.stderr.flush()
        answer = input().strip().lower() or 'c'
        if answer[:1] in ('c', 'r', 'q'):
            break
        print('Please answer c, r, or q...', file=sys.stderr)

    if answer[:1] == 'c':
        return 'continue'
    if answer[:1] == 'r':
        return 'replace'
    return None  # quit


def _check_config_drift(ctx: 'PodrunContext', action: str) -> Optional[str]:
    """Check for config drift on restart/attach and return the (possibly changed) action.

    Returns the action string (``'restart'``, ``'attach'``, ``'replace'``) or
    ``None`` (exit).  Only called when action is ``'restart'`` or ``'attach'``.
    """
    ns = ctx.ns
    name = ns.get('run.name')
    if not name or not ns.get('run.user_overlay'):
        return action

    changed = _detect_config_drift(ns)
    if not changed:
        return action

    # Auto-attach: warn and proceed (non-blocking for CI)
    if ns.get('run.auto_attach'):
        print(
            f'Warning: Config has changed since container {name!r} was created:',
            file=sys.stderr,
        )
        for f in changed:
            print(f'  modified: {f}', file=sys.stderr)
        print('  (--auto-attach: continuing with stale config)', file=sys.stderr)
        return action

    # Interactive prompt
    choice = _config_drift_prompt(name, changed, sys.stdin.isatty())
    if choice == 'continue':
        return action
    if choice == 'replace':
        return 'replace'
    return None  # quit


# ---------------------------------------------------------------------------
# Passthrough flag introspection
# ---------------------------------------------------------------------------


def _passthrough_has_flag(pt, prefix):
    """Return True if any arg in *pt* starts with *prefix* (e.g. ``--userns``)."""
    return any(a == prefix or a.startswith(prefix + '=') for a in pt)


def _passthrough_has_exact(pt, value):
    """Return True if the exact *value* string is in *pt*."""
    return value in pt


def _passthrough_has_short_flag(pt, char):
    """Check if short flag *char* is present (handles combined flags like ``-it``)."""
    for a in pt:
        if a.startswith('-') and not a.startswith('--') and '=' not in a:
            if char in a[1:]:
                return True
    return False


def _extract_passthrough_entrypoint(pt):
    """Extract and remove ``--entrypoint`` from passthrough args.

    Returns ``(entrypoint_value, filtered_pt)``.
    """
    alt_entrypoint = None
    filtered = []
    i = 0
    while i < len(pt):
        arg = pt[i]
        if arg.startswith('--entrypoint='):
            alt_entrypoint = arg.split('=', 1)[1]
        elif arg == '--entrypoint' and i + 1 < len(pt):
            alt_entrypoint = pt[i + 1]
            i += 2
            continue
        else:
            filtered.append(arg)
        i += 1
    return alt_entrypoint, filtered


def _extract_passthrough_user(pt):
    """Extract and remove ``--user``/``-u`` from passthrough args.

    Returns ``(user_value, filtered_pt)``.  Handles all forms produced by
    ``_PassthroughAction``: ``['--user', 'VAL']`` and ``['-u', 'VAL']``.
    """
    user_value = None
    filtered = []
    i = 0
    while i < len(pt):
        arg = pt[i]
        if arg == '--user' and i + 1 < len(pt):
            user_value = pt[i + 1]
            i += 2
            continue
        elif arg == '-u' and i + 1 < len(pt):
            user_value = pt[i + 1]
            i += 2
            continue
        else:
            filtered.append(arg)
        i += 1
    return user_value, filtered


def _validate_passthrough_user(user_value: str) -> None:
    """Validate that a ``--user`` value matches the host identity.

    Accepts: ``{UID}``, ``{UID}:{GID}``, ``{UNAME}``, ``{UNAME}:{GID}``.
    Exits with an error if the value conflicts.
    """
    valid = {
        str(UID),
        f'{UID}:{GID}',
        UNAME,
        f'{UNAME}:{GID}',
    }
    if user_value in valid:
        return
    print(
        f'Error: --user={user_value} conflicts with --user-overlay.\n'
        f'user-overlay maps host identity ({UNAME}, uid={UID}, gid={GID}).\n'
        f'Accepted values: {UID}, {UID}:{GID}, {UNAME}, {UNAME}:{GID}\n'
        'Remove --user-overlay or adjust --user to match.',
        file=sys.stderr,
    )
    sys.exit(1)


def _parse_mount_spec(mount_spec: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse a ``--mount`` spec string into ``(source, target)``.

    Handles standard key aliases: ``source``/``src`` and
    ``target``/``dst``/``destination``.
    """
    parts = dict(p.split('=', 1) for p in mount_spec.split(',') if '=' in p)
    source = parts.get('source') or parts.get('src')
    target = parts.get('target') or parts.get('dst') or parts.get('destination')
    return source, target


def _arg_mount_target(args: list, i: int) -> Tuple[Optional[str], int]:
    """Return ``(target, width)`` for the mount/volume arg at index *i*.

    *width* is 1 for equals form (``--mount=...``), 2 for space form
    (``--mount spec``).  Returns ``(None, 1)`` for non-mount args.
    """
    arg = args[i]
    mount_m = re.match(r'^--mount=(.*)', arg)
    if mount_m:
        return _parse_mount_spec(mount_m.group(1))[1], 1
    if arg == '--mount' and i + 1 < len(args):
        return _parse_mount_spec(args[i + 1])[1], 2
    vol_m = re.match(r'^(-v|--volume)=(.*)', arg)
    if vol_m:
        parts = _split_path_colon(vol_m.group(2))
        return (parts[1] if len(parts) >= 2 else None), 1
    if arg in ('-v', '--volume') and i + 1 < len(args):
        parts = _split_path_colon(args[i + 1])
        return (parts[1] if len(parts) >= 2 else None), 2
    return None, 1


def _volume_mount_destinations(*arg_lists) -> set:
    """Extract container destination paths from -v/--volume/--mount args across all arg lists."""
    dests = set()
    for args in arg_lists:
        i = 0
        while i < len(args):
            target, width = _arg_mount_target(args, i)
            if target:
                dests.add(re.sub(r'^~', f'/home/{UNAME}', target))
            i += width
    return dests


# ---------------------------------------------------------------------------
# Tilde expansion
# ---------------------------------------------------------------------------


def _expand_tilde_prefix(s: str, home: str) -> str:
    """Replace a leading ``~`` or ``~/`` with *home*."""
    if s == '~':
        return home
    if s.startswith('~/') or s.startswith('~\\'):
        return home + s[1:]
    return s


def _expand_tilde_spec(spec: str, first_home: str, second_home: str) -> str:
    """Expand ``~`` in a colon-separated path spec (``part0:part1[:rest]``).

    *first_home* is used for ``parts[0]``, *second_home* for ``parts[1]``.
    """
    parts = _split_path_colon(spec)
    if len(parts) >= 2:
        parts[0] = _expand_tilde_prefix(parts[0], first_home)
        parts[1] = _expand_tilde_prefix(parts[1], second_home)
    elif len(parts) == 1:
        parts[0] = _expand_tilde_prefix(parts[0], first_home)
    return ':'.join(parts)


def _process_vol_spec(
    spec: str,
    expand_tilde: bool,
    manifest_mounts: Optional[dict],
    container_home: str,
) -> Tuple[str, Optional[Tuple[str, str]], Optional[Tuple[str, str]]]:
    """Process a single ``src:dst[:opts]`` volume spec.

    Returns ``(new_spec, copy_staging_item, mount_entry)`` where
    *copy_staging_item* is ``(src, dst)`` if ``:0`` mode (the arg should
    be dropped), *mount_entry* is ``(dst, src)`` for the mount map, and
    *new_spec* is the rewritten spec string.
    """
    if expand_tilde:
        spec = _expand_tilde_spec(spec, USER_HOME, container_home)
    parts = _split_path_colon(spec)
    if len(parts) >= 3 and parts[-1] == '0':
        return '', (parts[0], parts[1]), None
    if manifest_mounts and len(parts) >= 2 and parts[0] in manifest_mounts:
        parts[0] = manifest_mounts[parts[0]]
    entry = (parts[1], parts[0]) if len(parts) >= 2 else None
    return ':'.join(parts), None, entry


def _process_mount_spec(
    spec: str,
    manifest_mounts: Optional[dict],
) -> Tuple[str, Optional[Tuple[str, str]]]:
    """Process a single ``--mount`` key=value spec.

    Returns ``(new_spec, mount_entry)`` where *mount_entry* is
    ``(target, source)`` for the mount map.
    """
    kvs = spec.split(',')
    source = target = None
    for j, kv in enumerate(kvs):
        if '=' in kv:
            key, val = kv.split('=', 1)
            if key in ('source', 'src'):
                if manifest_mounts and val in manifest_mounts:
                    val = manifest_mounts[val]
                source = val
                kvs[j] = f'{key}={val}'
            elif key in ('target', 'dst', 'destination'):
                target = val
    entry = (target, source) if source and target else None
    return ','.join(kvs), entry


def _process_volume_args(  # noqa: C901
    args: list,
    *,
    expand_tilde: bool = False,
    manifest_mounts: Optional[dict] = None,
) -> Tuple[list, list, dict]:
    """Single-pass volume/mount arg processing.

    Walks *args* once and for each ``-v``/``--volume``/``--mount`` entry:

    1. **Tilde expansion** (when *expand_tilde*) — ``~`` in source expands to
       ``USER_HOME``, in destination to ``/home/{UNAME}``.
    2. **``:0`` extraction** — items with ``:0`` mode are removed from the
       result and collected as copy-staging tuples.
    3. **Manifest translation** (when *manifest_mounts*) — if the source
       appears as a key in *manifest_mounts*, it is replaced with the
       daemon-visible path.
    4. **Mount map** — records ``{dest: source}`` for every volume/mount.

    Returns ``(processed_args, copy_staging_items, mount_map)``.
    """
    container_home = f'/home/{UNAME}'
    result: list = []
    copy_staging: list = []
    mount_map: dict = {}
    i = 0
    while i < len(args):
        arg = args[i]

        # --- -v= / --volume= equals form ---
        m = re.match(r'^(-v|--volume)=(.*)', arg)
        if m:
            spec, cs_item, mm_entry = _process_vol_spec(
                m.group(2),
                expand_tilde,
                manifest_mounts,
                container_home,
            )
            if cs_item:
                copy_staging.append(cs_item)
            else:
                if mm_entry:
                    mount_map[mm_entry[0]] = mm_entry[1]
                result.append(f'{m.group(1)}={spec}')
            i += 1
            continue

        # --- -v / --volume space form ---
        if arg in ('-v', '--volume') and i + 1 < len(args):
            spec, cs_item, mm_entry = _process_vol_spec(
                args[i + 1],
                expand_tilde,
                manifest_mounts,
                container_home,
            )
            if cs_item:
                copy_staging.append(cs_item)
            else:
                if mm_entry:
                    mount_map[mm_entry[0]] = mm_entry[1]
                result.append(arg)
                result.append(spec)
            i += 2
            continue

        # --- --mount= equals form ---
        mount_m = re.match(r'^(--mount)=(.*)', arg)
        if mount_m:
            spec, mm_entry = _process_mount_spec(mount_m.group(2), manifest_mounts)
            if mm_entry:
                mount_map[mm_entry[0]] = mm_entry[1]
            result.append(f'{mount_m.group(1)}={spec}')
            i += 1
            continue

        # --- --mount space form ---
        if arg == '--mount' and i + 1 < len(args):
            spec, mm_entry = _process_mount_spec(args[i + 1], manifest_mounts)
            if mm_entry:
                mount_map[mm_entry[0]] = mm_entry[1]
            result.append(arg)
            result.append(spec)
            i += 2
            continue

        result.append(arg)
        i += 1
    return result, copy_staging, mount_map


def _expand_export_tilde(exports: list) -> list:
    """Expand ``~`` in export entries (``container_path:host_path[:0]``).

    Host ``~`` expands to USER_HOME, container ``~`` expands to ``/home/{UNAME}``.
    """
    container_home = f'/home/{UNAME}'
    return [_expand_tilde_spec(e, container_home, USER_HOME) for e in exports]


# ---------------------------------------------------------------------------
# Entrypoint generation
# ---------------------------------------------------------------------------


def _lifecycle_command_to_shell(cmd, indent: str = '        ') -> str:
    """Convert a devcontainer lifecycle command to shell script lines.

    *cmd* may be a string, a list of strings, or a dict of named commands.
    Returns a shell snippet (with leading *indent*) or empty string for
    None/falsy input.

    - **string** → ``/bin/sh -c '<escaped>'``
    - **array**  → elements shlex-quoted and joined
    - **object** → each named command backgrounded (``&``), then ``wait``
    """
    if not cmd:
        return ''
    if isinstance(cmd, str):
        escaped = cmd.replace("'", "'\\''")
        return f"{indent}/bin/sh -c '{escaped}'\n"
    if isinstance(cmd, list):
        quoted = ' '.join(shlex.quote(str(c)) for c in cmd)
        return f'{indent}{quoted}\n'
    if isinstance(cmd, dict):
        lines = []
        for name, sub in cmd.items():
            sub_sh = _lifecycle_command_to_shell(sub, indent='').strip()
            if sub_sh:
                lines.append(f'{indent}# {name}')
                lines.append(f'{indent}{sub_sh} &')
        if lines:
            lines.append(f'{indent}wait')
        return '\n'.join(lines) + '\n' if lines else ''
    return ''


def _lifecycle_block(ns: dict, ns_key: str, label: str, indent: str = '        ') -> str:
    """Build a fault-tolerant guarded lifecycle shell block for *ns_key*.

    Returns empty string when the key is absent/falsy, otherwise wraps the
    command in a ``PODRUN_DEVCONTAINER_CLI`` guard and a
    ``_PODRUN_LIFECYCLE_OK`` check.  On failure the flag is set to ``0``
    and a warning is printed, but the entrypoint continues (the user still
    gets a shell).  Subsequent lifecycle blocks see the flag and skip.
    """
    cmd = ns.get(ns_key)
    shell = _lifecycle_command_to_shell(cmd, indent=indent + '    ')
    if not shell:
        return ''
    return (
        f'{indent}# Devcontainer lifecycle: {label}\n'
        f'{indent}if [ "$_PODRUN_LIFECYCLE_OK" = 1 ] && [ -z "${ENV_PODRUN_DEVCONTAINER_CLI}" ]; then\n'
        f'{indent}  (\n'
        f'{indent}    set -e\n'
        f'{shell}'
        f'{indent}  ) || {{ echo "podrun: warning: {label} failed" >&2; _PODRUN_LIFECYCLE_OK=0; }}\n'
        f'{indent}fi\n'
    )


def _run_host_command(cmd) -> subprocess.Popen:
    """Launch a single host-side command (string or list) and return its Popen."""
    if isinstance(cmd, str):
        return subprocess.Popen(cmd, shell=True)
    return subprocess.Popen([str(c) for c in cmd])


def _run_initialize_command(cmd) -> None:
    """Execute a devcontainer ``initializeCommand`` on the host.

    *cmd* may be a string, a list of strings, or a dict of named commands.
    Output streams directly to the terminal.  Exits with error on failure.
    """
    if not cmd:
        return
    if isinstance(cmd, dict):
        procs = {n: _run_host_command(s) for n, s in cmd.items() if s}
        failed = [n for n, p in procs.items() if p.wait() != 0]
        if failed:
            print(
                f'Error: initializeCommand failed for: {", ".join(failed)}',
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        rc = _run_host_command(cmd).wait()
        if rc != 0:
            print(f'Error: initializeCommand failed (exit {rc})', file=sys.stderr)
            sys.exit(1)


def generate_run_entrypoint(ns: dict, caps_to_drop: Optional[list] = None) -> str:
    """Generate the run-entrypoint script and return its path (SHA-named, idempotent).

    Reads from the *ns* dict: ``run.login``, ``run.shell``, ``run.export``.

    *caps_to_drop* is the list of capabilities to drop after entrypoint setup.
    Defaults to ``BOOTSTRAP_CAPS``.  Callers should filter out any caps the user
    explicitly requested via ``--cap-add`` or pass an empty list for ``--privileged``.
    """
    login_flag = ' -l' if ns.get('run.login') else ''
    if caps_to_drop is None:
        caps_to_drop = sorted(BOOTSTRAP_CAPS)
    default_shell = ns.get('run.shell')
    exports = sorted(ns.get('run.export') or [])

    # Build export blocks
    export_blocks = ''
    if exports:
        lines = []
        for entry in exports:
            src, _, copy_only = _parse_export(entry)
            staging = f'/.podrun/exports/{hashlib.sha256(src.encode()).hexdigest()[:12]}'
            mode = 'copy' if copy_only else 'mount'
            lines.append(f'        # Export ({mode}): {src}')
            if copy_only:
                lines.append(f'        if [ -d "{src}" ] && [ -d "{staging}" ]; then')
                lines.append(f'            if [ -z "$(ls -A "{staging}" 2>/dev/null)" ]; then')
                lines.append(f'                cp -a "{src}/." "{staging}/"')
                lines.append('            fi')
                lines.append(f'        elif [ -f "{src}" ] && [ -d "{staging}" ]; then')
                lines.append(f'            _dst="{staging}/$(basename "{src}")"')
                lines.append('            if [ ! -f "$_dst" ]; then')
                lines.append(f'                cp -a "{src}" "$_dst"')
                lines.append('            fi')
                lines.append('        fi')
            else:
                lines.append(f'        if [ -d "{src}" ] && [ -d "{staging}" ]; then')
                lines.append(f'            if [ -z "$(ls -A "{staging}" 2>/dev/null)" ]; then')
                lines.append(f'                cp -a "{src}/." "{staging}/"')
                lines.append('            fi')
                lines.append(f'            rm -rf "{src}"')
                lines.append(f'            ln -sfn "{staging}" "{src}"')
                lines.append(f'        elif [ -f "{src}" ] && [ -d "{staging}" ]; then')
                lines.append(f'            _dst="{staging}/$(basename "{src}")"')
                lines.append('            if [ ! -f "$_dst" ]; then')
                lines.append(f'                cp -a "{src}" "$_dst"')
                lines.append('            fi')
                lines.append(f'            rm -f "{src}"')
                lines.append(f'            ln -sfn "$_dst" "{src}"')
                lines.append(f'        elif [ -d "{staging}" ]; then')
                lines.append(f'            mkdir -p "$(dirname "{src}")"')
                lines.append(f'            ln -sfn "{staging}" "{src}"')
                lines.append('        fi')
            lines.append('')
        export_blocks = '\n'.join(lines)

    # Build lifecycle blocks (devcontainer lifecycle scripts)
    lifecycle_first_run = _lifecycle_block(
        ns, 'dc.on_create_command', 'onCreateCommand'
    ) + _lifecycle_block(ns, 'dc.post_create_command', 'postCreateCommand')
    lifecycle_post_start = _lifecycle_block(ns, 'dc.post_start_command', 'postStartCommand')
    lifecycle_post_attach = _lifecycle_block(ns, 'dc.post_attach_command', 'postAttachCommand')

    # 8-space indent to match textwrap.dedent template below
    if default_shell:
        shell_detect = (
            f'        # Use configured default shell\n'
            f'        if command -v {default_shell} > /dev/null 2>&1; then\n'
            f'          SHELL="$(command -v {default_shell})"; export SHELL\n'
            f'        else\n'
            f'          echo "podrun: warning: {default_shell} not found, falling back to sh" >&2\n'
            f'          SHELL="$(command -v sh)"; export SHELL\n'
            f'        fi'
        )
    else:
        shell_detect = (
            '        # Detect shell (prefer bash over sh)\n'
            '        if [ -z "$SHELL" ]; then SHELL="$(command -v sh)"; export SHELL; fi\n'
            '        if [ "$(basename "$SHELL")" = "sh" ]; then\n'
            '          if command -v bash > /dev/null 2>&1; then\n'
            '            SHELL="$(command -v bash)"; export SHELL\n'
            '          fi\n'
            '        fi'
        )

    # Build cap-drop + exec block (8-space indent to match template)
    if caps_to_drop:
        short_drop = ','.join(
            '-' + (c[4:] if c.startswith('CAP_') else c).lower() for c in caps_to_drop
        )
        long_drop = ','.join(
            '-cap_' + (c[4:] if c.startswith('CAP_') else c).lower() for c in caps_to_drop
        )
        cap_names = ' '.join(
            'cap_' + (c[4:] if c.startswith('CAP_') else c).lower() for c in caps_to_drop
        )
        cap_drop_block = (
            '        # Drop bootstrap capabilities before exec.\n'
            '        # Probe short names first (BusyBox), fall back to cap_ prefix (util-linux).\n'
            '        # Drop from both inheritable and ambient sets so effective caps\n'
            '        # are cleared after exec (ambient caps drive effective in userns).\n'
            '        if command -v setpriv > /dev/null 2>&1; then\n'
            f'          _drop="{short_drop}"\n'
            '          if ! setpriv --inh-caps="$_drop" --ambient-caps="$_drop" true 2>/dev/null; then\n'
            f'            _drop="{long_drop}"\n'
            '          fi\n'
            '          if [ $# -eq 0 ]; then\n'
            '            exec setpriv --inh-caps="$_drop" --ambient-caps="$_drop" $SHELL\n'
            '          else\n'
            '            exec setpriv --inh-caps="$_drop" --ambient-caps="$_drop" "$@"\n'
            '          fi\n'
            '        elif command -v capsh > /dev/null 2>&1; then\n'
            '          # capsh uses cap_xxx names; --delamb removes from ambient,\n'
            '          # --drop removes from bounding.\n'
            '          _capsh_args=""\n'
            '          # shellcheck disable=SC2043\n'
            f'          for _cap in {cap_names}; do\n'
            '            _capsh_args="$_capsh_args --delamb=$_cap --drop=$_cap"\n'
            '          done\n'
            '          # shellcheck disable=SC2086\n'
            '          if [ $# -eq 0 ]; then\n'
            '            exec capsh $_capsh_args -- -c "exec $SHELL"\n'
            '          else\n'
            '            _quoted=""\n'
            '            for _arg in "$@"; do\n'
            '              _quoted="$_quoted \'$_arg\'"\n'
            '            done\n'
            '            exec capsh $_capsh_args -- -c "exec $_quoted"\n'
            '          fi\n'
            '        else\n'
            '          if [ $# -eq 0 ]; then\n'
            '            exec $SHELL\n'
            '          else\n'
            '            exec "$@"\n'
            '          fi\n'
            '        fi'
        )
    else:
        cap_drop_block = (
            '        if [ $# -eq 0 ]; then\n'
            '          exec $SHELL\n'
            '        else\n'
            '          exec "$@"\n'
            '        fi'
        )

    script = textwrap.dedent(f'''\
        #!/bin/sh{login_flag}
        # Generated by podrun {__version__}. Do not modify by hand.
        set -e
        _PODRUN_LIFECYCLE_OK=1

{shell_detect}
        PODRUN_SHELL="$SHELL"; export PODRUN_SHELL

        # --- First-run setup (skipped on container restart) ---
        if [ ! -e {PODRUN_READY_PATH} ]; then

        # Remove image-shipped /etc/passwd entries that collide on UID but have
        # a different username (e.g. ubuntu:24.04 ships 'ubuntu' at UID 1000).
        # --passwd-entry appends our entry, but getpwuid returns the first match.
        # Same treatment for /etc/group (GID collision).
        #
        # Patch SHELL field in /etc/passwd (--passwd-entry creates the entry
        # with /bin/sh; update to the resolved shell path).
        #
        # Ensure passwd/group entries exist: some podman versions silently
        # ignore --passwd-entry when the UID already exists in the image,
        # so we add the entry ourselves as a fallback.
        #
        # IMPORTANT: Do NOT use sed -i.  Podman bind-mounts a generated
        # /etc/passwd into the container (--passwd-entry).  sed -i replaces
        # the file with a new inode, breaking the bind mount.  Podman's
        # exec -u resolution reads through the original mount, so it would
        # see the old (pre-sed) content while processes inside the container
        # see the new file.  Writing back via cat preserves the inode.
        if command -v sed > /dev/null 2>&1; then
          _t=$(mktemp)
          sed "/^{UNAME}:/!{{ /^[^:]*:[^:]*:{UID}:/d; }}" /etc/passwd > "$_t" 2>/dev/null && cat "$_t" > /etc/passwd || true
          sed "/^{UNAME}:/!{{ /^[^:]*:[^:]*:[^:]*:{GID}:/d; }}" /etc/group > "$_t" 2>/dev/null && cat "$_t" > /etc/group || true
          sed "s|^\\({UNAME}:.*:\\)/bin/sh\\$|\\1$SHELL|" /etc/passwd > "$_t" 2>/dev/null && cat "$_t" > /etc/passwd || true
          rm -f "$_t"
        fi
        if ! awk -v uid={UID} -F: '{{ if($3==uid){{found=1}} }} END{{exit !found}}' /etc/passwd 2>/dev/null; then
          echo "{UNAME}:*:{UID}:{GID}:{UNAME}:/home/{UNAME}:$SHELL" >> /etc/passwd 2>/dev/null || true
        fi
        if ! awk -v gid={GID} -F: '{{ if($3==gid){{found=1}} }} END{{exit !found}}' /etc/group 2>/dev/null; then
          echo "{UNAME}:x:{GID}:" >> /etc/group 2>/dev/null || true
        fi

        # Create home directory and populate from /etc/skel.
        # Skip entries whose destination is a bind mount (different device)
        # so user-mounted files (e.g. -v ~/.bashrc:/home/user/.bashrc) are
        # preserved.
        mkdir -p /home/{UNAME}
        if [ -d /etc/skel ]; then
          _home_dev=$(stat -c %d /home/{UNAME} 2>/dev/null) || _home_dev=""
          for _entry in /etc/skel/* /etc/skel/.[!.]* /etc/skel/..?*; do
            [ -e "$_entry" ] || continue
            _base="${{_entry##*/}}"
            _dest="/home/{UNAME}/$_base"
            if [ -e "$_dest" ] && [ -n "$_home_dev" ]; then
              _dest_dev=$(stat -c %d "$_dest" 2>/dev/null) || _dest_dev="$_home_dev"
              [ "$_dest_dev" != "$_home_dev" ] && continue
            fi
            cp -a "$_entry" "/home/{UNAME}/" 2>/dev/null || true
          done
        fi
        find /home/{UNAME} -xdev -exec chown {UID}:{GID} {{}} + 2>/dev/null || true

        # Copy-mode staging: host content staged :ro, copied here for writable access.
        # Each entry under /.podrun/copy-staging/ contains .podrun_target (destination)
        # and data (the actual file or directory content).  This is a no-op when
        # /.podrun/copy-staging/ is empty or absent.
        if [ -d /.podrun/copy-staging ]; then
          for _staging_entry in /.podrun/copy-staging/*; do
            [ -d "$_staging_entry" ] || continue
            _target="$(cat "$_staging_entry/.podrun_target" 2>/dev/null)" || continue
            if [ -d "$_staging_entry/data" ]; then
              mkdir -p "$_target"
              # shellcheck disable=SC2046
              chown $(stat -c "%u:%g" "$_staging_entry/data") "$_target" 2>/dev/null || true
              chmod "$(stat -c "%a" "$_staging_entry/data")" "$_target" 2>/dev/null || true
              cp -af "$_staging_entry/data/." "$_target/" 2>/dev/null || true
            else
              mkdir -p "$(dirname "$_target")"
              cp -af "$_staging_entry/data" "$_target" 2>/dev/null || true
            fi
            # Apply explicit permissions if .podrun_chmod descriptor exists
            if [ -f "$_staging_entry/.podrun_chmod" ]; then
              _chmod="$(cat "$_staging_entry/.podrun_chmod")"
              if [ -d "$_target" ]; then
                chmod -R "$_chmod" "$_target" 2>/dev/null || true
              else
                chmod "$_chmod" "$_target" 2>/dev/null || true
              fi
            fi
          done
        fi

        # Opportunistic sudo setup (requires CAP_DAC_OVERRIDE to write sudoers)
        if command -v sudo > /dev/null 2>&1; then
          echo "{UNAME} ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers 2>/dev/null || true
        fi

        # Git submodule support: bridge core.worktree paths.
        #
        # When the workspace is a git submodule, the root .git/ is mounted
        # at the correct relative position to the workspace (computed by
        # podrun based on submodule depth).  Relative gitdir: pointers in
        # nested .git files resolve correctly via the mount.
        #
        # However, nested submodule configs have core.worktree relative
        # paths that resolve to the host layout path, not the container
        # workspace.  A symlink bridges the gap.
        #
        # Self-contained: reads $PWD/.git, resolves the gitdir: pointer
        # using cd to find the mounted .git, then creates the symlink.
        if [ -f "$PWD/.git" ]; then
          _gitdir_rel="$(sed -n 's/^gitdir: *//p' "$PWD/.git")"
          _submod_path="${{_gitdir_rel##*.git/modules/}}"
          if [ "$_submod_path" != "$_gitdir_rel" ] && [ -n "$_submod_path" ]; then
            _git_prefix="${{_gitdir_rel%%/modules/*}}"
            _git_dir="$(cd "$PWD/$_git_prefix" 2>/dev/null && pwd)" || true
            if [ -d "$_git_dir" ]; then
              _git_parent="${{_git_dir%/.git}}"
              [ -z "$_git_parent" ] && _git_parent="/"
              mkdir -p "$_git_parent/$(dirname "$_submod_path")"
              ln -sfn "$PWD" "$_git_parent/$_submod_path"
            fi
          fi
        fi

        # Convenience symlink to workspace
        ln -s "$PWD" /home/{UNAME}/workdir > /dev/null 2>&1 || true

        # Wire rc.sh into bashrc
        _bashrc="/home/{UNAME}/.bashrc"
        if [ ! -f "$_bashrc" ] || ! grep -q '{PODRUN_RC_PATH}' "$_bashrc" 2>/dev/null; then
          echo '. {PODRUN_RC_PATH}' >> "$_bashrc"
        fi

{export_blocks}
{lifecycle_first_run}
        # Signal that first-run setup is complete.
        touch {PODRUN_READY_PATH}

        fi
        # --- End first-run setup ---

        # Force HOME — the image may have HOME baked in (e.g. ENV HOME=/root)
        # which prevents podman from deriving it from --passwd-entry.
        # Set on every start so restarted containers have correct env.
        HOME=/home/{UNAME}
        export HOME
        USER={UNAME}
        export USER
        ENV={PODRUN_RC_PATH}
        export ENV

{lifecycle_post_start}
{lifecycle_post_attach}
        # If an alternate entrypoint was requested (e.g. by the devcontainer
        # CLI via --entrypoint), prepend it to the args so it is exec'd after
        # our setup completes.  The podrun entrypoint always runs first to
        # ensure user identity and home directory are ready.
        if [ -n "$PODRUN_ALT_ENTRYPOINT" ]; then
          set -- "$PODRUN_ALT_ENTRYPOINT" "$@"
        fi

{cap_drop_block}
    ''')
    return _write_sha_file(script, 'entrypoint_', '.sh')


def generate_rc_sh(ns: dict) -> str:
    """Generate the rc.sh prompt/banner script and return its path (SHA-named, idempotent).

    Reads from the *ns* dict: ``run.prompt_banner``.
    """
    prompt_banner = ns.get('run.prompt_banner') or ns.get('run.image') or 'podrun'
    if _IS_WINDOWS:
        cpu_name = platform.processor() or 'Unknown CPU'
        cpu_vcount = str(os.cpu_count() or 1)
    else:
        cpu_name = run_os_cmd(
            "grep -m 1 'model name[[:space:]]*:' /proc/cpuinfo"
            " | cut -d ' ' -f 3- | sed 's/(R)/\u00ae/g; s/(TM)/\u2122/g;'"
        ).stdout.strip()
        cpu_vcount = run_os_cmd(
            "grep -o 'processor[[:space:]]*:' /proc/cpuinfo | wc -l"
        ).stdout.strip()
    cpu = f'{cpu_name} ({cpu_vcount} vCPU)'
    fl = 52
    cfl = fl + len(bytearray(cpu, sys.stdout.encoding or 'utf-8')) - len(cpu)

    script = textwrap.dedent(rf"""
        ##################################################################
        # Generated by podrun {__version__}. Do not modify by hand.

        # shellcheck disable=SC2148
        if [ -n "$PODRUN_STTY_INIT" ]; then
            stty $PODRUN_STTY_INIT > /dev/null 2>&1
            unset PODRUN_STTY_INIT
        fi
        unset PROMPT_COMMAND
        HOSTNAME="${{HOSTNAME:-{platform.node()}}}"
        export HOSTNAME
        _g=$(printf '\033[32m')
        _b=$(printf '\033[34m')
        _i=$(printf '\033[7m')
        _n=$(printf '\033[0m')
        _prompt_banner="{prompt_banner} 📦"
        _curr_shell="$(command -v "$0")"
        if readlink -f "$_curr_shell" > /dev/null 2>&1; then _curr_shell="$(readlink -f "$_curr_shell")"; fi
        case "$(basename "$_curr_shell")" in
          dash|ksh)
            _ps1_user="$(whoami)"
            PS1=$(printf "$_i$_prompt_banner$_n\n$_g$_ps1_user@$HOSTNAME$_n $_b\$PWD$_n\n\$ ") ;;
          *)
            PS1="$_i$_prompt_banner$_n\n$_g\u@\h$_n $_b\w$_n\n\$ " ;;
        esac
        # Skip banner for non-interactive shells.
        case "$-" in *i*) ;; *) return 0 2>/dev/null || : ;; esac
        _uptime="$(awk '{{ printf "%d", $1 }}' /proc/uptime)"
        _minutes=$((_uptime / 60))
        _hours=$((_minutes / 60))
        _minutes=$((_minutes % 60))
        _days=$((_hours / 24))
        _hours=$((_hours % 24))
        _weeks=$((_days / 7))
        _days=$((_days % 7))
        _uptime="up $_weeks weeks, $_days days, $_hours hours, $_minutes minutes"
        _mem_total=$(grep 'MemTotal:' /proc/meminfo | awk '{{ print $2 }}')
        _mem_avail=$(grep 'MemAvailable:' /proc/meminfo | awk '{{ print $2 }}')
        _mem_used=$((_mem_total - _mem_avail))
        _mem_used=$(awk -v mem_kb="$_mem_used" 'BEGIN{{ printf "%.1fG", mem_kb / 1000000}}')
        _mem_total=$(awk -v mem_kb="$_mem_total" 'BEGIN{{ printf "%.1fG", mem_kb / 1000000}}')
        _mem_avail=$(awk -v mem_kb="$_mem_avail" 'BEGIN{{ printf "%.1fG", mem_kb / 1000000}}')
        _mem="$_mem_used used, $_mem_total total ($_mem_avail avail)"
        _disk_free=$(df -h / | awk 'FNR == 2 {{ print $4 }}')
        _disk_used=$(df -h / | awk 'FNR == 2 {{ print $3 }}')
        cat << 'EOT'
                         ,,))))))));,
                      __)))))))))))))),
           \|/       -\(((((''''((((((((.     .----------------------------.
           -*-==//////((''  .     `)))))),   /  PODRUN __________________)
           /|\      ))| o    ;-.    '(((((  /            _______________)   ,(,
                    ( `|    /  )    ;))))' /         _______________)    ,_))^;(~
                       |   |   |   ,))((((_/      ________) __          %,;(;(>';'~
                       o_);   ;    )))(((`    \ \   ~---~  `:: \       %%~~)(v;(`('~
                             ;    ''''````         `:       `:: |\,__,%%    );`'; ~ %
                            |   _                )     /      `:|`----'     `-'
                      ______/\/~    |                 /        /
                    /~;;.____/;;'  /          ___--,-(   `;;;/
                   / //  _;______;'------~~~~~    /;;/\    /
                  //  | |                        / ;   \;;,\
                 (<_  | ;                      /',/-----'  _>
                  \_| ||_                     //~;~~~~~~~~~
        EOT
        echo "$_g─────────────╴$_n\`\-| $_g─────────────────$_n \(,~~ $_g─────────────────────────────────────"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━$_n \~| $_g━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        printf "┃$_n    CPU $_g┃$_n %-{cfl}.{cfl}s $_g┃$_n  DISK SPACE  $_g┃\\n" "{cpu}"
        printf "┃$_n    RAM $_g┃$_n %-{fl}.{fl}s $_g┃$_n free  %6s $_g┃\\n" "$_mem" "$_disk_free"
        printf "┃$_n UPTIME $_g┃$_n %-{fl}.{fl}s $_g┃$_n used  %6s $_g┃$_n\\n" "$_uptime" "$_disk_used"
    """).lstrip('\n')

    return _write_sha_file(script, 'rc_', '.sh')


def generate_exec_entrypoint(ns: Optional[dict] = None) -> str:
    """Generate exec-entrypoint.sh and return its path (SHA-named, idempotent).

    The exec-entrypoint is mostly configuration-independent — it reads
    ``PODRUN_*`` env vars at runtime (set by ``podman run -e``).  When *ns*
    is provided, ``dc.post_attach_command`` is injected before the exec block.
    """
    lifecycle_post_attach = _lifecycle_block(
        ns or {}, 'dc.post_attach_command', 'postAttachCommand'
    )

    script = textwrap.dedent(f"""\
        #!/bin/sh
        # Generated by podrun {__version__}. Do not modify by hand.
        _PODRUN_LIFECYCLE_OK=1

        # --- Wait for run-entrypoint.sh setup ---
        # The run-entrypoint creates the user, home directory, exports, etc.
        # If exec races the entrypoint, those resources may not exist yet.
        # Wait for the READY sentinel that run-entrypoint touches after setup.
        while [ ! -e {PODRUN_READY_PATH} ]; do
          sleep 0.1
        done

        # --- HOME resolution ---
        # The image may bake in ENV HOME=/root which podman exec inherits.
        # Read HOME from /etc/passwd (set by --passwd-entry) to override it.
        _home="$(awk -v uid="$(id -u)" -F: '$3==uid{{print $6}}' /etc/passwd 2>/dev/null)"
        if [ -n "$_home" ] && [ -d "$_home" ]; then
          HOME="$_home"; export HOME
        fi

        # --- Shell resolution ---
        # Priority: $1 arg -> $PODRUN_SHELL -> /etc/passwd -> /bin/sh
        # Then prefer bash over sh (matches run-entrypoint.sh logic).
        _shell="${{1:-}}"
        if [ -z "$_shell" ]; then
          _shell="${{PODRUN_SHELL:-}}"
        fi
        if [ -z "$_shell" ]; then
          _shell="$(awk -v uid="$(id -u)" -F: '$3==uid{{print $NF}}' /etc/passwd 2>/dev/null)"
        fi
        if [ -z "$_shell" ] || ! command -v "$_shell" > /dev/null 2>&1; then
          _shell="/bin/sh"
        fi
        if [ "$(basename "$_shell")" = "sh" ]; then
          if command -v bash > /dev/null 2>&1; then
            _shell="$(command -v bash)"
          fi
        fi
        SHELL="$_shell"; export SHELL

        # --- Login resolution ---
        # Priority: $2 arg -> $PODRUN_LOGIN -> 0 (no login)
        _login="${{2:-}}"
        if [ -z "$_login" ]; then
          _login="${{PODRUN_LOGIN:-0}}"
        fi

        # --- stty resize ---
        if [ -n "$PODRUN_STTY_INIT" ]; then
          stty $PODRUN_STTY_INIT > /dev/null 2>&1
          unset PODRUN_STTY_INIT
        fi

{lifecycle_post_attach}
        # --- Exec ---
        shift 2 2>/dev/null || true
        if [ $# -gt 0 ]; then
          exec "$@"
        elif [ "$_login" = "1" ]; then
          exec "$SHELL" -l
        else
          exec "$SHELL"
        fi
    """)
    return _write_sha_file(script, 'exec_entry_', '.sh')


# ---------------------------------------------------------------------------
# Overlay arg builders
# ---------------------------------------------------------------------------


def compute_caps_to_drop(pt):
    """Compute bootstrap caps to drop, filtering out user --cap-add overlaps.

    Returns an empty list if ``--privileged`` is in passthrough (all caps
    retained).  Otherwise returns ``BOOTSTRAP_CAPS`` minus any caps the
    user explicitly added via ``--cap-add``.
    """
    if _passthrough_has_exact(pt, '--privileged'):
        return []
    # Collect user-requested caps from --cap-add=X and --cap-add X
    user_caps = set()
    i = 0
    while i < len(pt):
        arg = pt[i]
        if arg.startswith('--cap-add='):
            for cap in arg.split('=', 1)[1].split(','):
                user_caps.add(cap.strip().upper())
        elif arg == '--cap-add' and i + 1 < len(pt):
            for cap in pt[i + 1].split(','):
                user_caps.add(cap.strip().upper())
            i += 2
            continue
        i += 1
    return sorted(c for c in BOOTSTRAP_CAPS if c not in user_caps)


def _user_overlay_args(ns, pt, entrypoint_path, rc_path, exec_entry_path):
    """Build args for --user-overlay: map host user identity into container."""
    args = []
    if not _passthrough_has_flag(pt, '--userns'):
        args.append('--userns=keep-id')
    # Explicit --user ensures Config.User in `podman inspect` reports the
    # mapped UID, not 0:0.  Without this, podman-remote (Windows) and tools
    # like VS Code's devcontainer CLI that read Config.User to determine
    # the container user would incorrectly use root.
    args.append(f'--user={UID}:{GID}')
    if not _passthrough_has_flag(pt, '--passwd-entry'):
        args.append(f'--passwd-entry={UNAME}:*:{UID}:{GID}:{UNAME}:/home/{UNAME}:/bin/sh')
    caps_to_drop = compute_caps_to_drop(pt)
    for cap in BOOTSTRAP_CAPS:
        args.append(f'--cap-add={cap}')
    args.append(f'--entrypoint={PODRUN_ENTRYPOINT_PATH}')
    args.append(f'-v={entrypoint_path}:{PODRUN_ENTRYPOINT_PATH}:ro,z')
    args.append(f'-v={rc_path}:{PODRUN_RC_PATH}:ro,z')
    args.append(f'-v={exec_entry_path}:{PODRUN_EXEC_ENTRY_PATH}:ro,z')
    # Explicit HOME ensures Config.Env in `podman inspect` reports the
    # correct home directory.  Without this, podman derives HOME from
    # Config.WorkingDir (the -w flag), causing tools like VS Code's
    # devcontainer CLI to place .vscode-server in the workspace folder
    # instead of the user's home.
    args.append(f'--env=HOME=/home/{UNAME}')
    args.append(f'--env=ENV={PODRUN_RC_PATH}')
    for entry in ns.get('run.export') or []:
        container_path, host_path, _ = _parse_export(entry)
        abs_host = os.path.abspath(host_path)
        try:
            os.makedirs(abs_host, exist_ok=True)
        except PermissionError:
            print(
                f'Error: cannot create export directory {abs_host}\n'
                f'  Permission denied for export {entry!r}.',
                file=sys.stderr,
            )
            sys.exit(1)
        staging_hash = hashlib.sha256(container_path.encode()).hexdigest()[:12]
        args.append(f'-v={abs_host}:/.podrun/exports/{staging_hash}:z')
    return args, caps_to_drop


def _interactive_overlay_args(ns, pt):
    """Build args for --interactive-overlay: interactive session flags."""
    args = []
    if not (_passthrough_has_short_flag(pt, 'i') or _passthrough_has_short_flag(pt, 't')):
        args.append('-it')
    args.append('--detach-keys=ctrl-q,ctrl-q')
    if not _passthrough_has_exact(pt, '--init'):
        args.append('--init')
    return args


def _resolve_git_submodule(workspace_src: str) -> Optional[str]:
    """If workspace_src/.git is a submodule pointer file, return the resolved git dir.

    Returns None if .git is a directory (normal repo) or doesn't exist.
    """
    dot_git = os.path.join(workspace_src, '.git')
    if not os.path.isfile(dot_git):
        return None
    with open(dot_git) as f:
        content = f.read().strip()
    if not content.startswith('gitdir:'):
        return None
    rel_path = content[len('gitdir:') :].strip()
    return str(pathlib.Path(dot_git).parent.joinpath(rel_path).resolve())


def _find_root_git_dir(git_dir: str) -> Tuple[Optional[str], Optional[str]]:
    """Find the root ``.git/`` directory from a resolved submodule git dir.

    Git stores nested submodule git dirs under the root repo's ``.git/modules/``
    tree.  This function walks up *git_dir* to find the enclosing ``.git``
    directory component and returns ``(root_git_dir, subpath)`` where *subpath*
    is the relative path from the root ``.git/`` to *git_dir*.

    Returns ``(None, None)`` if no ``.git`` component is found.
    """
    parts = pathlib.PurePosixPath(git_dir).parts
    for i, part in enumerate(parts):
        if part == '.git':
            root = str(pathlib.PurePosixPath(*parts[: i + 1]))
            subpath = str(pathlib.PurePosixPath(*parts[i + 1 :])) if i + 1 < len(parts) else ''
            return root, subpath
    return None, None


def _git_submodule_args(workspace_src: str, workspace_folder: str) -> list:
    """Return podman args to mount the root ``.git/`` when *workspace_src* is a submodule.

    The container mount target is computed dynamically based on the submodule
    depth and the *workspace_folder* path.  The ``gitdir:`` relative pointer
    in the ``.git`` file has a fixed number of ``../`` determined by the host
    layout.  Walking up *workspace_folder* by the same count gives the correct
    mount point so the pointer resolves inside the container.

    The entrypoint creates a worktree bridge symlink so that ``core.worktree``
    relative paths in nested submodule configs also resolve correctly.
    No ``GIT_DIR``/``GIT_WORK_TREE`` env vars are needed.

    In nested-remote mode, local gitdir resolution may fail because the
    pointer references a host path that doesn't exist inside the container.
    In that case, the mount manifest from the outer podrun is consulted to
    propagate any ``.git`` mount with its daemon-visible source path.

    Returns an empty list for normal repos or when the gitdir pointer is broken.
    """
    git_dir = _resolve_git_submodule(workspace_src)
    if not git_dir or not os.path.isdir(git_dir):
        # In nested-remote mode, the gitdir pointer references a host path
        # that doesn't exist here.  Check the manifest for a .git mount
        # that the outer podrun created.
        if _staging_dir() != _daemon_dir():
            return _git_submodule_args_from_manifest()
        return []
    root_git, subpath = _find_root_git_dir(git_dir)
    if not root_git or not subpath or not os.path.isdir(root_git):
        return []
    # subpath is like 'modules/simulation/plato/libs'; strip 'modules/' prefix
    # to get the submodule's repo-relative path, then compute depth.
    if not subpath.startswith('modules/'):
        return []
    submod_repo_path = subpath[len('modules/') :]
    depth = len(pathlib.PurePosixPath(submod_repo_path).parts)
    # Walk workspace_folder up by depth to find the container mount parent.
    # PurePosixPath.parent stops at '/' (POSIX semantics), matching how
    # ../../.. past / resolves to / in the container filesystem.
    container_parent = pathlib.PurePosixPath(workspace_folder)
    for _ in range(depth):
        container_parent = container_parent.parent
    container_git_mount = str(container_parent / '.git')
    return [f'-v={root_git}:{container_git_mount}:z']


def _git_submodule_args_from_manifest() -> list:
    """Propagate ``.git`` mounts from the outer podrun via the mount manifest.

    Scans the manifest's ``mounts`` section for any destination ending in
    ``/.git`` and re-emits them using the daemon-visible source path.
    """
    manifest = _read_mount_manifest()
    args: list = []
    for dest, source in manifest.get('mounts', {}).items():
        if dest.endswith('/.git'):
            args.append(f'-v={source}:{dest}:z')
    return args


def _host_overlay_args(ns, pt):
    """Build args for --host-overlay: overlay host system context onto container."""
    args = []
    if not _passthrough_has_flag(pt, '--hostname'):
        args.append(f'--hostname={platform.node()}')
    # Skip --network=host on Windows: on podman machine "host" means the VM's
    # network (not the Windows host), and it conflicts with --userns=keep-id
    # (kernel refuses sysfs remount when user ns doesn't own network ns).
    if not _IS_WINDOWS and not _passthrough_has_flag(pt, '--network'):
        args.append('--network=host')
    if not _passthrough_has_exact(pt, '--security-opt=seccomp=unconfined'):
        args.append('--security-opt=seccomp=unconfined')
    # Auto workspace: only when -w is not already in passthrough (from dc_run_args
    # or devcontainer CLI).  When -w is present, the workspace is already configured.
    if not _passthrough_has_flag(pt, '-w') and not _passthrough_has_flag(pt, '--workdir'):
        workspace_folder = ns.get('dc.workspace_folder') or '/app'
        if workspace_folder not in _volume_mount_destinations(pt):
            args.append(f'-v={pathlib.Path.cwd()}:{workspace_folder}:z')
        args.append(f'-w={workspace_folder}')
        if not ns.get('run.no_auto_resolve_git_submodules'):
            args.extend(_git_submodule_args(str(pathlib.Path.cwd()), workspace_folder))
    if not _passthrough_has_exact(pt, '--env=TERM=xterm-256color'):
        args.append('--env=TERM=xterm-256color')
    if os.path.exists('/etc/localtime'):
        args.append('-v=/etc/localtime:/etc/localtime:ro')
    return args


def _copy_staging_args(items: list, chmod_map: Optional[dict] = None) -> list:
    """Build staging dirs and podman volume args for copy-mode items.

    *items* is a list of ``(host_path, container_path)`` tuples.

    For each item a staging directory is created under :func:`_staging_dir`
    containing a ``.podrun_target`` file (the container destination) and a
    ``data`` entry (the actual content).  The staging directory is mounted
    ``:ro`` into ``/.podrun/copy-staging/{sha12}``; the entrypoint copies
    its contents to the target path so the container has a writable copy.

    If *chmod_map* is provided, it maps container paths to octal mode
    strings (e.g. ``{'~/.ssh': '700'}``).  A ``.podrun_chmod`` file is
    written into the staging directory; the entrypoint applies
    ``chmod [-R]`` after copying.

    For **files**, the host file is copied into the staging dir at build
    time (self-contained — one mount).  For **directories**, a nested bind
    mount provides the content (two mounts: one for the target metadata,
    one for the data directory).  In nested-remote mode, the general
    ``_translate_nested_volume_sources`` pass rewrites the directory source
    to the daemon-visible path — no special handling here.
    """
    write_base = _staging_dir()
    daemon_base = _daemon_dir()
    args: list = []
    for host_path, container_path in items:
        sha12 = hashlib.sha256(container_path.encode()).hexdigest()[:12]
        staging_dir = os.path.join(write_base, 'copy-staging', sha12)
        daemon_staging_dir = os.path.join(daemon_base, 'copy-staging', sha12)
        container_staging = f'/.podrun/copy-staging/{sha12}'
        pathlib.Path(staging_dir).mkdir(parents=True, exist_ok=True)

        # Write the target path descriptor
        target_file = os.path.join(staging_dir, '.podrun_target')
        with open(target_file, 'w', encoding='utf-8', newline='\n') as f:
            f.write(container_path)

        # Write optional chmod descriptor (Windows only — NTFS has no Unix
        # permission bits so bind-mounted files appear as 0777; on Linux the
        # stat-based copy already preserves the correct source permissions).
        if _IS_WINDOWS and chmod_map and container_path in chmod_map:
            chmod_file = os.path.join(staging_dir, '.podrun_chmod')
            with open(chmod_file, 'w', encoding='utf-8', newline='\n') as f:
                f.write(chmod_map[container_path])

        if os.path.isfile(host_path):
            # File: copy into staging/data at build time (one mount).
            # Use shutil.copy (not copy2) so the data file gets the current
            # mtime — copy2 preserves the source mtime.
            data_path = os.path.join(staging_dir, 'data')
            shutil.copy(host_path, data_path)
            args.append(f'-v={daemon_staging_dir}:{container_staging}:ro,z')
        elif os.path.isdir(host_path):
            # Directory: bind-mount the host dir as staging/data (two mounts).
            # In nested-remote mode, _translate_nested_volume_sources will
            # rewrite host_path to the daemon-visible source.
            args.append(f'-v={daemon_staging_dir}:{container_staging}:ro,z')
            args.append(f'-v={host_path}:{container_staging}/data:ro,z')
    return args


def _dot_files_overlay_args(ns, pt):
    """Build args for --dot-files-overlay.

    Returns items from ``_DOTFILES`` whose host path exists.  Tilde
    expansion and ``:0`` → copy-staging resolution happen downstream.
    """
    args = []
    for arg in _DOTFILES:
        # Extract host path from -v=host:ctr:mode
        m = re.match(r'^-v=([^:]+):', arg)
        if m and os.path.exists(os.path.expanduser(m.group(1))):
            args.append(arg)
    return args


def _x11_args(ns):
    """Build args for X11 socket and xauth forwarding."""
    args = []
    x11_socket = pathlib.Path('/tmp/.X11-unix')
    if x11_socket.exists():
        result = run_os_cmd('xauth info | grep "Authority file" | awk \'{ print $3 }\'')
        if result.returncode == 0 and result.stdout.strip():
            xauth_path = result.stdout.strip()
            args.append('--env=DISPLAY')
            args.append('-v=/tmp/.X11-unix:/tmp/.X11-unix:ro')
            args.append(f'-v={xauth_path}:/home/{UNAME}/.Xauthority:ro')
    return args


def _podman_remote_args(ns):
    """Build args for podman-remote (rootless Podman socket passthrough).

    On Windows, podman machine handles the remote connection — skip Unix
    socket mounting and just forward ``CONTAINER_HOST`` if set.
    """
    args = []
    if _IS_WINDOWS:
        container_host = os.environ.get('CONTAINER_HOST')
        if container_host:
            args.append(f'--env=CONTAINER_HOST={container_host}')
        return args
    store_socket = ns.get('run.store_socket')
    if store_socket and pathlib.Path(store_socket).exists():
        args.append(f'-v={store_socket}:{PODRUN_SOCKET_PATH}')
        args.append(f'--env=CONTAINER_HOST={PODRUN_CONTAINER_HOST}')
    else:
        podman_socket = f'/run/user/{UID}/podman/podman.sock'
        if pathlib.Path(podman_socket).exists():
            args.append(f'-v={podman_socket}:{PODRUN_SOCKET_PATH}')
            args.append(f'--env=CONTAINER_HOST={PODRUN_CONTAINER_HOST}')
        else:
            print(
                'Warning: podman remote was requested but podman.socket not found.', file=sys.stderr
            )
            print('systemctl --user enable --now podman.socket', file=sys.stderr)
    return args


def _env_args(ns):
    """Build container environment and PODRUN_* environment variable args."""
    args = []
    for key, val in (ns.get('run.container_env') or {}).items():
        args.append(f'--env={key}={val}')

    # Canonical "inside a podrun container" marker.
    args.append(f'--env={ENV_PODRUN_CONTAINER}=1')

    # Contract: when the outer podrun used --podman-remote, tell the inner
    # podrun so _default_podman_path() resolves to podman-remote.
    if ns.get('run.podman_remote'):
        args.append(f'--env={ENV_PODRUN_PODMAN_REMOTE}=1')

    if ns.get('internal.dc_from_cli'):
        args.append(f'--env={ENV_PODRUN_DEVCONTAINER_CLI}=1')

    overlays = sorted([name for ns_key, name in _OVERLAY_FIELDS if ns.get(ns_key)])
    overlay_str = ','.join(overlays) if overlays else 'none'
    args.append(f'--env={ENV_PODRUN_OVERLAYS}={overlay_str}')

    if ns.get('run.host_overlay'):
        workspace_folder = ns.get('dc.workspace_folder') or '/app'
        args.append(f'--env={ENV_PODRUN_WORKDIR}={workspace_folder}')
    if ns.get('run.shell'):
        args.append(f'--env={ENV_PODRUN_SHELL}={ns["run.shell"]}')
    if ns.get('run.login') is not None:
        args.append(f'--env={ENV_PODRUN_LOGIN}={"1" if ns["run.login"] else "0"}')

    image = ns.get('run.image')
    if image:
        repo, name, tag = _parse_image_ref(image)
        args.append(f'--env={ENV_PODRUN_IMG}={image}')
        args.append(f'--env={ENV_PODRUN_IMG_NAME}={name}')
        args.append(f'--env={ENV_PODRUN_IMG_REPO}={repo}')
        args.append(f'--env={ENV_PODRUN_IMG_TAG}={tag}')
    return args


def _validate_overlay_args(ns):
    """Error on args that conflict with enabled overlays.

    Note: ``--user``/``-u`` validation is handled separately by
    :func:`_extract_passthrough_user` + :func:`_validate_passthrough_user`
    in :func:`build_overlay_run_command`, which also translates matching
    values to the canonical ``--user={UID}:{GID}`` form.
    """
    if not ns.get('run.user_overlay'):
        return
    all_args = ns.get('run.passthrough_args') or []

    for arg in all_args:
        m = re.match(r'--userns=(.*)', arg)
        if m and m.group(1) != 'keep-id':
            print(
                f"Warning: {arg} overrides --user-overlay's --userns=keep-id.\n"
                'User identity mapping may not work correctly.',
                file=sys.stderr,
            )
            break


def print_overlays():
    """Print each overlay group and its constituent settings."""
    print('Overlay groups:')
    print()
    print('  user:')
    print('    --userns=keep-id')
    print('    --user=<uid>:<gid>')
    print('    --passwd-entry=<user>:*:<uid>:<gid>:<user>:/home/<user>:/bin/sh')
    print(f'    --cap-add={",".join(BOOTSTRAP_CAPS)}  (dropped after entrypoint)')
    print(f'    --entrypoint={PODRUN_ENTRYPOINT_PATH}')
    print(f'    -v=<run-entrypoint>:{PODRUN_ENTRYPOINT_PATH}:ro,z')
    print(f'    -v=<rc.sh>:{PODRUN_RC_PATH}:ro,z')
    print(f'    -v=<exec-entrypoint>:{PODRUN_EXEC_ENTRY_PATH}:ro,z')
    print()
    print('  host (implies user):')
    print('    --user-overlay')
    print(f'    --hostname={platform.node()}')
    print('    --network=host')
    print('    --security-opt=seccomp=unconfined')
    print('    -v=<cwd>:<workspaceFolder>')
    print('    -w=<workspaceFolder>')
    print('    --env=TERM=xterm-256color')
    print()
    print('  interactive:')
    print('    -it')
    print('    --init')
    print('    --detach-keys=ctrl-q,ctrl-q')
    print()
    print('  dotfiles (implies user):')
    print('    --user-overlay')
    for arg in _DOTFILES:
        print(f'    {arg}  (if exists)')
    print()
    print('  session (implies host + interactive + dotfiles):')
    print('    --host-overlay')
    print('    --interactive-overlay')
    print()
    print('  adhoc (implies session):')
    print('    --session')
    print('    --rm')
    print()


# ---------------------------------------------------------------------------
# devcontainer.json discovery and parsing
# ---------------------------------------------------------------------------


def find_devcontainer_json(start_dir=None):
    """Walk upward looking for devcontainer.json (standard, shorthand, named configs)."""
    start = pathlib.Path(start_dir) if start_dir else pathlib.Path.cwd()
    for path in [start, *start.parents]:
        # 1. Standard location
        candidate = path / '.devcontainer' / 'devcontainer.json'
        if candidate.exists():
            return candidate
        # 2. Root-level shorthand
        candidate = path / '.devcontainer.json'
        if candidate.exists():
            return candidate
        # 3. Named configurations (.devcontainer/<subfolder>/devcontainer.json)
        devcontainer_dir = path / '.devcontainer'
        if devcontainer_dir.is_dir():
            for child in sorted(devcontainer_dir.iterdir()):
                if child.is_dir():
                    candidate = child / 'devcontainer.json'
                    if candidate.exists():
                        return candidate
    return None


def _strip_jsonc(text: str) -> str:
    """Strip // and /* */ comments (not inside strings) and trailing commas."""
    result = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            # consume entire string literal
            j = i + 1
            while j < n:
                if text[j] == '\\':
                    j += 2
                    continue
                if text[j] == '"':
                    j += 1
                    break
                j += 1
            result.append(text[i:j])
            i = j
        elif c == '/' and i + 1 < n and text[i + 1] == '/':
            # line comment -- skip to end of line
            i += 2
            while i < n and text[i] != '\n':
                i += 1
        elif c == '/' and i + 1 < n and text[i + 1] == '*':
            # block comment -- skip to closing */
            i += 2
            while i + 1 < n and not (text[i] == '*' and text[i + 1] == '/'):
                i += 1
            i += 2
        else:
            result.append(c)
            i += 1
    # remove trailing commas before } or ]
    cleaned = ''.join(result)
    cleaned = re.sub(r',\s*([}\]])', r'\1', cleaned)
    return cleaned


def parse_devcontainer_json(path):
    """Parse devcontainer.json from path (None, file, or directory)."""
    if path is None:
        return {}
    p = pathlib.Path(path)
    if p.is_dir():
        candidate = p / 'devcontainer.json'
        if candidate.exists():
            p = candidate
        else:
            p = find_devcontainer_json(p)
            if p is None:
                print(f'Error: no devcontainer.json found under {path}', file=sys.stderr)
                sys.exit(1)
    text = p.read_text()
    return json.loads(_strip_jsonc(text))


def extract_podrun_config(devcontainer: dict) -> dict:
    """Extract customizations.podrun from a devcontainer dict."""
    return devcontainer.get('customizations', {}).get('podrun', {})  # type: ignore[no-any-return]


def devcontainer_run_args(dc: dict, ns: dict) -> list:  # noqa: C901
    """Convert devcontainer.json top-level fields to podman run args.

    Returns only git-submodule args when the devcontainer CLI is driving
    (``ns['internal.dc_from_cli']``), since the CLI already emitted the
    standard dc fields but doesn't handle git submodule mounts.
    """
    args: list = []
    workspace_mount = dc.get('workspaceMount')

    # Git submodule: always emitted (devcontainer CLI doesn't handle this)
    if not ns.get('run.no_auto_resolve_git_submodules'):
        wm_source = _parse_mount_spec(workspace_mount)[0] if workspace_mount else None
        workspace_folder = dc.get('workspaceFolder')
        if wm_source and workspace_folder:
            args.extend(_git_submodule_args(wm_source, workspace_folder))

    if ns.get('internal.dc_from_cli'):
        return args

    for mount in dc.get('mounts', []):
        if isinstance(mount, dict):
            parts = ','.join(f'{k}={v}' for k, v in mount.items())
            args.append(f'--mount={parts}')
        else:
            args.append(f'--mount={mount}')

    for cap in dc.get('capAdd', []):
        args.append(f'--cap-add={cap}')

    for opt in dc.get('securityOpt', []):
        args.append(f'--security-opt={opt}')

    if dc.get('privileged', False):
        args.append('--privileged')

    if dc.get('init', False):
        args.append('--init')

    env = {**dc.get('containerEnv', {}), **dc.get('remoteEnv', {})}
    for key, val in env.items():
        args.append(f'--env={key}={val}')

    if workspace_mount:
        args.append(f'--mount={workspace_mount}')

    workspace_folder = dc.get('workspaceFolder')
    if workspace_folder:
        args.append(f'-w={workspace_folder}')

    args.extend(dc.get('runArgs', []))

    return args


# ---------------------------------------------------------------------------
# Store management
# ---------------------------------------------------------------------------

_PODRUN_STORES_DIR = (
    os.path.join(tempfile.gettempdir(), 'podrun-stores') if _IS_WINDOWS else '/tmp/podrun-stores'
)


def _store_hash(graphroot: str) -> str:
    """Return a truncated SHA-256 hash for *graphroot*.

    Used by ``_runroot_path``, ``_store_socket_path``, and
    ``_store_pid_path`` to derive a deterministic, short directory
    name that stays well within the 108-byte ``sun_path`` limit.
    """
    return hashlib.sha256(graphroot.encode()).hexdigest()[:12]


def _runroot_path(graphroot: str) -> str:
    """Return a deterministic runroot path under ``/tmp`` for *graphroot*."""
    return f'{_PODRUN_STORES_DIR}/{_store_hash(graphroot)}'


def _store_socket_path(graphroot: str) -> str:
    """Return the podman service socket path for a store."""
    return f'{_PODRUN_STORES_DIR}/{_store_hash(graphroot)}/podman.sock'


def _store_pid_path(graphroot: str) -> str:
    """Return the PID file path for a store's podman service."""
    return f'{_PODRUN_STORES_DIR}/{_store_hash(graphroot)}/podman.pid'


def _socket_is_alive(sock, pid_file):
    """Return True if the podman service is still running."""
    if not os.path.exists(pid_file):
        return False
    try:
        pid = int(pathlib.Path(pid_file).read_text().strip())
        os.kill(pid, 0)  # Check if process exists
        return os.path.exists(sock)
    except (ValueError, OSError):
        return False


def _wait_for_socket(sock, timeout=10):
    """Block until the socket file appears or timeout."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(sock):
            return
        time.sleep(0.1)
    print(f'Warning: timed out waiting for {sock}', file=sys.stderr)


def _ensure_store_service(graphroot, runroot, store_dir=None, podman_path='podman'):
    """Ensure a podman system service is running for the given store.

    Starts ``podman system service`` bound to the store's graphroot/runroot
    with no idle timeout, writing its PID to a file for cleanup.
    Returns the socket path.
    """
    if _is_remote(podman_path):
        print(
            'Error: cannot start store service with podman-remote.\n'
            'The store lives on the host — use local podman to manage it.',
            file=sys.stderr,
        )
        sys.exit(1)

    sock = _store_socket_path(graphroot)
    pid_file = _store_pid_path(graphroot)

    # Already running?
    if _socket_is_alive(sock, pid_file):
        return sock

    # Clean up stale socket
    if os.path.exists(sock):
        os.unlink(sock)

    # Build env with registries.conf if present
    env = os.environ.copy()
    if store_dir:
        reg = pathlib.Path(store_dir) / 'registries.conf'
        if reg.exists():
            env['CONTAINERS_REGISTRIES_CONF'] = str(reg)

    cmd = [
        podman_path,
        '--root',
        graphroot,
        '--runroot',
        runroot,
        '--storage-driver',
        'overlay',
        'system',
        'service',
        '--time',
        '0',
        f'unix://{sock}',
    ]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Write PID
    pathlib.Path(pid_file).write_text(str(proc.pid))

    # Wait for socket to appear
    _wait_for_socket(sock)

    return sock


def _default_store_dir():
    """Walk upward from cwd looking for a devcontainer project root.

    If ``.devcontainer/`` dir found → ``<root>/.devcontainer/.podrun/store``.
    Else if ``.devcontainer.json`` found → ``<root>/.podrun/store``.
    If no project root exists, returns ``None``.
    """
    start = pathlib.Path.cwd()
    for path in [start, *start.parents]:
        if (path / '.devcontainer').is_dir():
            return str(path / '.devcontainer' / '.podrun' / 'store')
        if (path / '.devcontainer.json').exists():
            return str(path / '.podrun' / 'store')
    return None


def _store_init(store_dir: str) -> None:
    """Create a project-local podrun store (graphroot + runroot symlink)."""
    store_path = pathlib.Path(store_dir).resolve()
    graphroot = store_path / 'graphroot'
    try:
        graphroot.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        print(
            f'Error: cannot create local store at {store_path}\n'
            f'  Permission denied creating {graphroot}\n'
            f'  Check directory permissions or use --local-store-ignore to skip.',
            file=sys.stderr,
        )
        sys.exit(1)

    # Runroot under /tmp (deterministic, short path)
    runroot_target = _runroot_path(str(graphroot))
    try:
        pathlib.Path(runroot_target).mkdir(parents=True, exist_ok=True)
    except PermissionError:
        print(
            f'Error: cannot create runroot at {runroot_target}\n'
            f'  Permission denied. Check /tmp permissions.',
            file=sys.stderr,
        )
        sys.exit(1)

    _ensure_runroot_symlink(store_path, runroot_target)


def _ensure_runroot_symlink(store_path: pathlib.Path, runroot: str) -> None:
    """Create or update the convenience symlink store_dir/runroot → runroot."""
    link = store_path / 'runroot'
    try:
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(runroot)
    except OSError:
        pass


def _store_print_info(store_dir: str) -> None:
    """Print summary information about a podrun store."""
    store_path = pathlib.Path(store_dir).resolve()
    display = str(store_path)
    graphroot = store_path / 'graphroot'

    if not graphroot.is_dir():
        print(f'Local store: {display} (not initialized)')
        return

    graphroot_str = str(graphroot)
    runroot = _runroot_path(graphroot_str)
    runroot_exists = os.path.isdir(runroot)

    print(f'Local store: {display}')
    print(f'  graphroot: {graphroot_str}')
    runroot_status = '' if runroot_exists else '  (missing — will be created on use)'
    print(f'    runroot: {runroot}{runroot_status}')


def _stop_store_service(graphroot: str) -> None:
    """Stop the podman system service for a store, if running."""
    pid_file = _store_pid_path(graphroot)
    if not os.path.exists(pid_file):
        return
    try:
        pid = int(pathlib.Path(pid_file).read_text().strip())
        os.kill(pid, signal.SIGTERM)
    except (ValueError, OSError):
        pass
    try:
        os.unlink(pid_file)
    except OSError:
        pass
    sock = _store_socket_path(graphroot)
    try:
        os.unlink(sock)
    except OSError:
        pass


def _store_destroy(store_dir: str, podman_path: str) -> None:  # noqa: C901
    """Remove a project-local podrun store and its runroot."""
    store_path = pathlib.Path(store_dir).resolve()
    if not store_path.exists():
        return

    # Read runroot symlink target before removing
    runroot_link = store_path / 'runroot'
    runroot_target = None
    if runroot_link.is_symlink():
        try:
            runroot_target = os.readlink(str(runroot_link))
        except OSError:
            pass

    # Let podman clean up overlay layers (UID-mapped files) before rm.
    # Reset every graphroot directory found in the store.
    runroot_targets = set()
    if runroot_target:
        runroot_targets.add(runroot_target)
    for gr in sorted(store_path.glob('graphroot*')):
        if not gr.is_dir():
            continue
        _stop_store_service(str(gr))
        gr_runroot = _runroot_path(str(gr))
        runroot_targets.add(gr_runroot)
        try:
            subprocess.run(
                [
                    podman_path,
                    '--root',
                    str(gr),
                    '--runroot',
                    gr_runroot,
                    '--storage-driver',
                    'overlay',
                    'system',
                    'reset',
                    '--force',
                ],
                capture_output=True,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass

    # Remove the store directory.  Overlay storage may contain UID-mapped
    # files inaccessible outside podman's user namespace, so fall back to
    # ``podman unshare rm -rf`` when shutil.rmtree hits PermissionError.
    try:
        shutil.rmtree(str(store_path))
    except PermissionError:
        subprocess.run(
            [podman_path, 'unshare', 'rm', '-rf', str(store_path)],
            capture_output=True,
            timeout=120,
        )
        if store_path.exists():
            print(f'Error: failed to remove {store_path}', file=sys.stderr)
            sys.exit(1)
    print(f'Removed {store_path}')

    # Remove associated runroot directories
    for rt in sorted(runroot_targets):
        if os.path.exists(rt):
            shutil.rmtree(rt)
            print(f'Removed {rt}')

    # Clean up parent if empty
    parent = pathlib.Path(_PODRUN_STORES_DIR)
    if parent.exists() and not any(parent.iterdir()):
        parent.rmdir()
        print(f'Removed {parent} (empty)')


def _resolve_store(ctx: 'PodrunContext') -> Tuple[List[str], dict]:  # noqa: C901
    """Resolve store directory into podman global flags.

    Returns ``(flags_list, env_dict)`` where *flags_list* contains
    ``['--root', ..., '--runroot', ...]`` (and ``--storage-driver`` when
    not already supplied via podman global args) or empty if no store is
    active.

    If ``--storage-driver`` is already present in ``podman_global_args``
    (i.e. the user passed it explicitly), that value is respected and
    the local store does not inject a redundant ``--storage-driver``.
    """
    ns = ctx.ns

    # --local-store-ignore → skip store entirely
    if ns.get('root.local_store_ignore'):
        return [], {}

    store_dir = ns.get('root.local_store')

    # Env var fallback: PODRUN_LOCAL_STORE (between config and auto-discovery)
    if not store_dir:
        store_dir = os.environ.get('PODRUN_LOCAL_STORE')
        if store_dir:
            ns['root.local_store'] = store_dir

    # Auto-discover if not explicitly set
    if not store_dir:
        store_dir = _default_store_dir()
        ns['root.local_store'] = store_dir

    # No project root found — no default store
    if not store_dir:
        return [], {}

    # Destroy store if requested — wipe before checking graphroot so the
    # existing auto-init / uninitialised logic handles post-destroy state.
    if ns.get('root.local_store_destroy'):
        _store_destroy(store_dir, ctx.podman_path)

    store_path = pathlib.Path(store_dir).resolve()
    graphroot = store_path / 'graphroot'

    if not graphroot.is_dir():
        if ns.get('root.local_store_auto_init'):
            _store_init(store_dir)
        else:
            # No initialized store — clear and return empty
            ns['root.local_store'] = None
            return [], {}

    graphroot_str = str(graphroot)
    runroot = _runroot_path(graphroot_str)
    try:
        pathlib.Path(runroot).mkdir(parents=True, exist_ok=True)
    except PermissionError:
        print(
            f'Error: cannot create runroot at {runroot}\n'
            f'  Permission denied. Check /tmp permissions.',
            file=sys.stderr,
        )
        sys.exit(1)

    _ensure_runroot_symlink(store_path, runroot)

    # Conflict check: error if podman_global_args already has --root/--runroot
    pga = ns.get('podman_global_args') or []
    conflicts = {'--root', '--runroot'}
    found = conflicts.intersection(pga)
    if found:
        print(
            f'Error: local store conflicts with explicit podman flags: {", ".join(sorted(found))}\n'
            f'Use --local-store-ignore to suppress local store.',
            file=sys.stderr,
        )
        sys.exit(1)

    flags = ['--root', graphroot_str, '--runroot', runroot]

    # Only inject --storage-driver if the user hasn't already provided one
    # via podman global args.  Prefer the devcontainer/config-script value
    # (root.storage_driver) over the default 'overlay'.
    if '--storage-driver' not in pga:
        driver = ns.get('root.storage_driver') or 'overlay'
        flags.extend(['--storage-driver', driver])

    return flags, {}


def _is_vacant_store(storage_dir: pathlib.Path) -> bool:
    """Return True if *storage_dir* is a vacant podman store.

    A "vacant" store is scaffolding created by commands like ``podman ps``
    but contains no pulled images.  Detection: podman creates a
    ``{driver}-images/`` directory (e.g. ``overlay-images/``) on first
    image pull.  If no such directory exists, the store is vacant.
    """
    try:
        for entry in storage_dir.iterdir():
            if entry.is_dir() and entry.name.endswith('-images'):
                return False
    except OSError:
        return False
    return True


def _nfs_remediate(ctx: 'PodrunContext') -> None:  # noqa: C901
    """Detect/remediate NFS-mounted podman storage by symlinking to local disk.

    Called from ``main()`` between ``resolve_config()`` and ``_apply_store()``.
    No-op when running as a remote client or nested inside a podrun container.
    The default mode ``error`` detects NFS and reports it; other modes take
    corrective action.
    """
    ns = ctx.ns
    mode = ns.get('root.nfs_remediate') or 'init'
    if _is_remote(ctx.podman_path):
        return
    if os.environ.get(ENV_PODRUN_CONTAINER):
        return

    storage_dir = pathlib.Path(USER_HOME) / '.local' / 'share' / 'containers' / 'storage'
    base = ns.get('root.nfs_remediate_path') or _NFS_REMEDIATE_DEFAULT_BASE
    user_store = pathlib.Path(base) / UNAME

    # Already a symlink — check target matches.
    if storage_dir.is_symlink():
        target = str(pathlib.Path(os.readlink(str(storage_dir))).resolve())
        expected = str(user_store.resolve()) if user_store.exists() else str(user_store)
        if target == expected:
            return
        print(
            f'Warning: {storage_dir} is already a symlink to {target} (expected {user_store})',
            file=sys.stderr,
        )
        return

    # Not on a network filesystem — nothing to do.
    if not _is_network_fs(str(storage_dir)):
        return

    # Error mode: detect NFS and report, take no action.
    if mode == 'error':
        print(
            f'Error: {storage_dir} is on a network filesystem.\n'
            f'  Podman storage is incompatible with NFS. Use --nfs-remediate=init\n'
            f'  to create a symlink to local disk, or see podrun docs for other modes.',
            file=sys.stderr,
        )
        sys.exit(1)

    # Ensure base directory exists (may need sudo for /opt).
    base_path = pathlib.Path(base)
    if not base_path.is_dir():
        ret = subprocess.run(
            ['sudo', 'mkdir', '-p', str(base_path)],
            capture_output=True,
            text=True,
        )
        if ret.returncode != 0:
            print(
                f'Error: failed to create {base_path} (sudo mkdir failed).\n'
                f'  {ret.stderr.strip()}\n'
                f'  Hint: use --nfs-remediate-path to specify a writable directory.',
                file=sys.stderr,
            )
            sys.exit(1)
        subprocess.run(
            ['sudo', 'chmod', '1777', str(base_path)],
            capture_output=True,
            text=True,
        )

    # Ensure user subdirectory exists.
    user_store.mkdir(parents=True, exist_ok=True)

    # Handle existing real directory.
    # Vacant stores (scaffolding from e.g. `podman ps` but no images) are
    # removed silently — they have no data worth preserving.
    if storage_dir.is_dir():
        if _is_vacant_store(storage_dir):
            shutil.rmtree(str(storage_dir))
        elif mode == 'init':
            print(
                f'Error: {storage_dir} exists as a real directory on NFS.\n'
                f'  Use --nfs-remediate=mv to move contents, or --nfs-remediate=rm to remove.',
                file=sys.stderr,
            )
            sys.exit(1)
        elif mode == 'mv':
            print(f'Moving {storage_dir} contents to {user_store} ...', file=sys.stderr)
            for item in storage_dir.iterdir():
                dest = user_store / item.name
                if dest.exists():
                    print(f'  skip (exists): {item.name}', file=sys.stderr)
                    continue
                print(f'  moving: {item.name}', file=sys.stderr)
                shutil.move(str(item), str(dest))
            shutil.rmtree(str(storage_dir))
        elif mode == 'rm':
            print(f'Removing {storage_dir} ...', file=sys.stderr)
            shutil.rmtree(str(storage_dir))
        elif mode == 'prompt':
            is_interactive = sys.stdin.isatty()
            if not is_interactive:
                print(
                    f'Error: {storage_dir} exists as a real directory on NFS.\n'
                    f'  Non-interactive session — use --nfs-remediate=mv or --nfs-remediate=rm.',
                    file=sys.stderr,
                )
                sys.exit(1)
            print(f'\n{storage_dir} exists as a real directory on NFS.', file=sys.stderr)
            if yes_no_prompt('Move contents to local storage?', True, True):
                print(f'Moving {storage_dir} contents to {user_store} ...', file=sys.stderr)
                for item in storage_dir.iterdir():
                    dest = user_store / item.name
                    if dest.exists():
                        print(f'  skip (exists): {item.name}', file=sys.stderr)
                        continue
                    print(f'  moving: {item.name}', file=sys.stderr)
                    shutil.move(str(item), str(dest))
                shutil.rmtree(str(storage_dir))
            elif yes_no_prompt('Remove existing storage?', False, True):
                shutil.rmtree(str(storage_dir))
            else:
                print('Cancelled.', file=sys.stderr)
                sys.exit(0)

    # Ensure parent directory exists and create symlink.
    storage_dir.parent.mkdir(parents=True, exist_ok=True)
    storage_dir.symlink_to(user_store)
    print(f'Created symlink: {storage_dir} -> {user_store}', file=sys.stderr)


def _apply_store(ctx: 'PodrunContext') -> None:
    """Resolve store, prepend flags, and handle store-only exits.

    When using ``podman-remote`` the store concept does not apply —
    the store filesystem lives on the host and ``_resolve_store`` must not
    run (it would walk the mounted workspace looking for ``.devcontainer``
    and attempt ``mkdir`` on non-existent paths).  Any flags that *do* get
    injected into ``podman_global_args`` are filtered for binary
    compatibility by ``_filter_global_args`` in ``main()``.
    """
    ns = ctx.ns
    remote = _is_remote(ctx.podman_path)

    if ns.get('root.local_store_destroy') and remote:
        print('Error: --local-store-destroy not supported with podman remote', file=sys.stderr)
        sys.exit(1)

    if not remote:
        flags, _env = _resolve_store(ctx)
        if flags:
            existing = ns.get('podman_global_args') or []
            ns['podman_global_args'] = flags + existing

    # If destroy, exit if there is nothing else to do
    if ns.get('root.local_store_destroy'):
        if ns['subcommand'] is None and not ns.get('root.local_store_info'):
            sys.exit(0)

    if ns.get('root.local_store_info'):
        if remote:
            print('Local store: disabled (podman remote)', file=sys.stderr)
        else:
            store_dir = ns.get('root.local_store')
            if store_dir:
                _store_print_info(store_dir)
            else:
                print('No local store configured.', file=sys.stderr)
        sys.exit(0)


# ---------------------------------------------------------------------------
# Config key mapping and merge
# ---------------------------------------------------------------------------

_ROOT_CONFIG_MAP = {
    'localStore': 'root.local_store',
    'localStoreAutoInit': 'root.local_store_auto_init',
    'localStoreIgnore': 'root.local_store_ignore',
    'noPodrunrc': 'root.no_podrunrc',
    'storageDriver': 'root.storage_driver',
    'nfsRemediate': 'root.nfs_remediate',
    'nfsRemediatePath': 'root.nfs_remediate_path',
}

_RUN_CONFIG_MAP = {
    'name': 'run.name',
    'userOverlay': 'run.user_overlay',
    'hostOverlay': 'run.host_overlay',
    'interactiveOverlay': 'run.interactive_overlay',
    'session': 'run.session',
    'adhoc': 'run.adhoc',
    'x11': 'run.x11',
    'podmanRemote': 'run.podman_remote',
    'shell': 'run.shell',
    'login': 'run.login',
    'promptBanner': 'run.prompt_banner',
    'autoAttach': 'run.auto_attach',
    'autoReplace': 'run.auto_replace',
    'fuseOverlayfs': 'run.fuse_overlayfs',
    'dotFilesOverlay': 'run.dot_files_overlay',
    'noAutoResolveGitSubmodules': 'run.no_auto_resolve_git_submodules',
    'exports': 'run.export',
}

# Top-level devcontainer.json fields → ns['dc.*'] keys.
# These are resolved in resolve_config after variable expansion.
_DC_CONFIG_MAP = {
    'name': 'dc.name',
    'workspaceMount': 'dc.workspace_mount',
    'workspaceFolder': 'dc.workspace_folder',
    'containerEnv': 'dc.container_env',
    'remoteEnv': 'dc.remote_env',
    'image': 'dc.image',
    'initializeCommand': 'dc.initialize_command',
    'onCreateCommand': 'dc.on_create_command',
    'postCreateCommand': 'dc.post_create_command',
    'postStartCommand': 'dc.post_start_command',
    'postAttachCommand': 'dc.post_attach_command',
}


def _devcontainer_to_ns(podrun_cfg: dict) -> dict:
    """Convert customizations.podrun to namespace-keyed dict (non-None only)."""
    result = {}
    for json_key, ns_key in _ROOT_CONFIG_MAP.items():
        val = podrun_cfg.get(json_key)
        if val is not None:
            result[ns_key] = val
    for json_key, ns_key in _RUN_CONFIG_MAP.items():
        val = podrun_cfg.get(json_key)
        if val is not None:
            result[ns_key] = val
    return result


def _resolve_dc_fields(dc: dict, ns: dict, dc_path: Optional[str] = None) -> None:
    """Expand devcontainer variables and resolve fields to ``ns['dc.*']``.

    No-op when *dc* is empty.  Performs variable expansion on *dc* in-place,
    then maps top-level devcontainer.json fields to ``ns['dc.*']`` keys via
    ``_DC_CONFIG_MAP``.  ``dc.workspace_folder`` gets special handling:
    workspaceMount target takes priority over the workspaceFolder field.
    """
    if not dc:
        return

    # Variable expansion
    if dc_path:
        project_dir = _devcontainer_project_dir(dc_path)
        # First pass: expand workspaceFolder (it can reference localWorkspaceFolder vars)
        var_context = {
            'localWorkspaceFolder': project_dir or '',
            'containerWorkspaceFolder': '',
        }
        if 'workspaceFolder' in dc:
            dc['workspaceFolder'] = _expand_devcontainer_vars(dc['workspaceFolder'], var_context)
        # Second pass: use resolved containerWorkspaceFolder for remaining fields
        var_context['containerWorkspaceFolder'] = dc.get('workspaceFolder', '')
        for field in (
            'name',
            'workspaceMount',
            'mounts',
            'runArgs',
            'containerEnv',
            'remoteEnv',
            'customizations',
            'initializeCommand',
            'onCreateCommand',
            'postCreateCommand',
            'postStartCommand',
            'postAttachCommand',
        ):
            if field in dc:
                dc[field] = _expand_devcontainer_vars(dc[field], var_context)

    # Map dc fields to ns['dc.*']
    for dc_key, ns_key in _DC_CONFIG_MAP.items():
        val = dc.get(dc_key)
        if val is not None:
            ns[ns_key] = val

    # workspace_folder override: workspaceMount target takes priority
    ws_mount = dc.get('workspaceMount')
    if ws_mount:
        _, target = _parse_mount_spec(ws_mount)
        if target:
            ns['dc.workspace_folder'] = target


# ---------------------------------------------------------------------------
# Devcontainer variable expansion
# ---------------------------------------------------------------------------

_DC_VAR_RE = re.compile(r'\$\{([^}]+)\}')


def _devcontainer_project_dir(dc_path) -> Optional[str]:
    """Derive the project root directory from a devcontainer.json path.

    Returns None if *dc_path* is None.  Always returns an absolute path
    so that ``${localWorkspaceFolder}`` is unambiguous — relative paths
    would be resolved by the daemon relative to *its* CWD, which breaks
    nested-remote mode.
    """
    if dc_path is None:
        return None
    p = pathlib.Path(dc_path).resolve()
    if not p.is_file():
        return str(p)
    # Walk up looking for .devcontainer parent dir
    for parent in (p.parent, p.parent.parent):
        if parent.name == '.devcontainer':
            return str(parent.parent)
    # .devcontainer.json shorthand: file is at project root
    if p.name == '.devcontainer.json':
        return str(p.parent)
    # Explicit path: use parent directory
    return str(p.parent)


def _expand_devcontainer_vars(value, context: dict):  # noqa: C901
    """Expand devcontainer.json variables in a string value.

    Recursively walks dicts and lists.  Non-string/dict/list values are
    returned unchanged.

    Context keys:

    * ``localWorkspaceFolder``     — host path containing devcontainer.json
    * ``containerWorkspaceFolder`` — container path (workspaceFolder value)
    """
    if isinstance(value, str):

        def _replace(m):
            expr = m.group(1)
            if expr == 'localWorkspaceFolder':
                return context.get('localWorkspaceFolder', '')
            if expr == 'localWorkspaceFolderBasename':
                lwf = context.get('localWorkspaceFolder', '')
                return os.path.basename(lwf) if lwf else ''
            if expr == 'containerWorkspaceFolder':
                return context.get('containerWorkspaceFolder', '')
            if expr == 'containerWorkspaceFolderBasename':
                cwf = context.get('containerWorkspaceFolder', '')
                return os.path.basename(cwf) if cwf else ''
            if expr.startswith('localEnv:'):
                rest = expr[len('localEnv:') :]
                if ':' in rest:
                    var, default = rest.split(':', 1)
                    return os.environ.get(var, default)
                return os.environ.get(rest, '')
            if expr.startswith('containerEnv:'):
                # Container env vars are only available at runtime; leave as-is
                return m.group(0)
            if expr == 'devcontainerId':
                lwf = context.get('localWorkspaceFolder', '')
                return hashlib.sha256(lwf.encode()).hexdigest()[:16]
            # Unknown variable — leave as-is
            return m.group(0)

        return _DC_VAR_RE.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _expand_devcontainer_vars(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_devcontainer_vars(item, context) for item in value]
    return value


_UNSUPPORTED_DC_LIFECYCLE_FIELDS = (
    'updateContentCommand',
    'waitFor',
)


def _warn_unsupported_lifecycle_fields(dc: dict) -> None:
    """Warn about devcontainer lifecycle fields that podrun does not execute."""
    found = [f for f in _UNSUPPORTED_DC_LIFECYCLE_FIELDS if f in dc]
    if found:
        names = ', '.join(found)
        print(
            f'podrun: warning: devcontainer.json contains unsupported lifecycle'
            f' field(s): {names} (ignored by podrun, use the devcontainer CLI'
            f' for full lifecycle support)',
            file=sys.stderr,
        )


def _load_devcontainer(ns) -> Tuple[dict, dict, Optional[str]]:
    """Load devcontainer.json, expand variables, and resolve to ``ns['dc.*']``.

    Returns ``(dc, podrun_cfg, dc_path)``.  When ``root.no_devconfig`` is set
    or no devcontainer.json is found, both dicts are empty and dc_path is None.

    When the devcontainer CLI is driving (detected via the
    ``devcontainer.config_file`` label), ``ns['internal.dc_from_cli']`` is set
    to ``True``.  The dc dict is still fully parsed and ``dc.*`` namespace
    fields are populated (for internal use like ``PODRUN_WORKDIR``).  Callers
    that emit podman args from dc fields must check this flag to avoid
    duplicating args the CLI already passed in.
    """
    if ns.get('root.no_devconfig'):
        return {}, {}, None

    # Check for label-based dc selection (devcontainer CLI passes this label)
    label_config_path = None
    for lbl in ns.get('run.label') or []:
        key, _, value = lbl.partition('=')
        if key == 'devcontainer.config_file':
            label_config_path = value
            # Mark that devcontainer CLI is driving
            ns['internal.dc_from_cli'] = True

    if ns.get('root.config'):
        dc_path = ns['root.config']
    elif label_config_path:
        dc_path = label_config_path
    else:
        dc_path = find_devcontainer_json()

    dc = parse_devcontainer_json(dc_path) if dc_path is not None else {}
    dc_path_str = str(dc_path) if dc_path is not None else None
    _resolve_dc_fields(dc, ns, dc_path_str)

    podrun_cfg = extract_podrun_config(dc)
    return dc, podrun_cfg, dc_path_str


def _discover_podrunrc() -> Optional[str]:
    """Glob ``~/.podrunrc*`` and return the single match, or None.

    Exits with error if multiple matches are found.
    """
    candidates = [p for p in pathlib.Path(USER_HOME).glob('.podrunrc*') if not p.is_dir()]
    if not candidates:
        return None
    if len(candidates) > 1:
        names = ', '.join(sorted(p.name for p in candidates))
        print(f'Error: multiple ~/.podrunrc* files found: {names}', file=sys.stderr)
        print('Only one is allowed. Remove extras and retry.', file=sys.stderr)
        sys.exit(1)
    return str(candidates[0])


def _collect_script_config(ctx: 'PodrunContext', podrun_cfg, flags) -> Tuple[dict, list, list]:
    """Find and execute config scripts, return ``(script_ns, script_passthrough, paths)``."""
    ns = ctx.ns
    script_paths: list = []
    dc_script = podrun_cfg.get('configScript')
    if dc_script:
        script_paths.extend([dc_script] if isinstance(dc_script, str) else dc_script)
    cli_scripts = ns.get('root.config_script')
    if cli_scripts:
        script_paths.extend(cli_scripts)

    if not script_paths:
        return {}, [], []

    script_tokens = run_config_scripts(script_paths, ctx=ctx)
    ns_dict, pt = parse_config_tokens(script_tokens, flags)
    return ns_dict, pt, script_paths


def _apply_run_specifics(ns, ctx: 'PodrunContext', dc_ns, script_ns, rc_ns=None):
    """Apply run-subcommand-specific merges: overlays, image fallback, exports.

    All dc top-level fields are already resolved to ``ns['dc.*']`` by
    ``resolve_config`` before this function is called.
    """
    if rc_ns is None:
        rc_ns = {}

    # Overlay implication chain: adhoc→session→host+interactive+dotfiles→user
    if ns.get('run.adhoc'):
        ns['run.session'] = True
    if ns.get('run.session'):
        ns['run.host_overlay'] = True
        ns['run.interactive_overlay'] = True
        ns['run.dot_files_overlay'] = True
    if ns.get('run.host_overlay'):
        ns['run.user_overlay'] = True
    if ns.get('run.dot_files_overlay'):
        ns['run.user_overlay'] = True

    # containerEnv + remoteEnv from devcontainer.json.
    # In podrun both map to --env on `podman run`; remoteEnv wins on conflict.
    dc_container_env = ns.get('dc.container_env') or {}
    dc_remote_env = ns.get('dc.remote_env') or {}
    merged_env = {**dc_container_env, **dc_remote_env}
    if merged_env:
        ns['run.container_env'] = merged_env

    # Image/command resolution: CLI trailing > devcontainer image
    dc_image = ns.get('dc.image')
    if not ctx.trailing_args and dc_image:
        ctx.trailing_args = [dc_image]

    # Exports append: rc + dc + script + cli, with tilde expansion
    rc_exports = rc_ns.get('run.export') or []
    dc_exports = dc_ns.get('run.export') or []
    script_exports = script_ns.get('run.export') or []
    cli_exports = ns.get('run.export') or []
    combined_exports = rc_exports + dc_exports + script_exports + cli_exports
    if combined_exports:
        ns['run.export'] = _expand_export_tilde(combined_exports)


def resolve_config(ctx: 'PodrunContext', flags=None) -> 'PodrunContext':  # noqa: C901
    """Four-way merge: CLI > config-script > devcontainer.json > ~/.podrunrc*.

    Updates ctx.ns in place and attaches context.
    """

    def _first(*values):
        for v in values:
            if v is not None:
                return v
        return None

    ns = ctx.ns

    # 1–3. Load devcontainer.json, expand variables, resolve to ns['dc.*']
    dc, podrun_cfg, dc_path = _load_devcontainer(ns)
    ns['internal.config_dc_path'] = dc_path

    # Copy dc_from_cli from ns (set by _load_devcontainer) to ctx
    ctx.dc_from_cli = ns.get('internal.dc_from_cli', False)

    # Warn about unsupported devcontainer lifecycle fields when running standalone
    if dc and not ctx.dc_from_cli:
        _warn_unsupported_lifecycle_fields(dc)

    # 4. Discover and execute ~/.podrunrc* (lowest priority config)
    rc_ns: dict = {}
    rc_passthrough: list = []
    rc_path: Optional[str] = None
    no_podrunrc = ns.get('root.no_podrunrc') or podrun_cfg.get('noPodrunrc')
    if not no_podrunrc:
        rc_path = _discover_podrunrc()
        if rc_path:
            rc_tokens = run_config_scripts([rc_path], ctx=ctx)
            rc_ns, rc_passthrough = parse_config_tokens(rc_tokens, flags)
    ns['internal.config_rc_path'] = rc_path

    # 5–6. Determine and execute config scripts
    script_ns, script_passthrough, config_script_paths = _collect_script_config(
        ctx, podrun_cfg, flags
    )
    ns['internal.config_script_paths'] = config_script_paths

    # 7. Convert devcontainer config → _devcontainer_to_ns() + devcontainer_run_args()
    dc_ns = _devcontainer_to_ns(podrun_cfg)
    dc_run_args = devcontainer_run_args(dc, ns)

    # 8. Merge scalars — _first(cli_ns, script_ns, dc_ns, rc_ns) per key
    #    List-append keys (run.export) are handled in _apply_run_specifics.
    _APPEND_KEYS = {'run.export'}
    all_keys = set()
    for k in ns:
        if k.startswith('root.') or k.startswith('run.'):
            all_keys.add(k)
    all_keys.update(rc_ns.keys())
    all_keys.update(script_ns.keys())
    all_keys.update(dc_ns.keys())

    for key in all_keys:
        if key in _APPEND_KEYS:
            continue
        cli_val = ns.get(key)
        script_val = script_ns.get(key)
        dc_val = dc_ns.get(key)
        rc_val = rc_ns.get(key)
        merged = _first(cli_val, script_val, dc_val, rc_val)
        if merged is not None:
            ns[key] = merged

    # 9. Prepend podman args — rc first (lowest priority), then DC, script,
    #    then CLI passthrough (already in the list, highest priority).
    existing_passthrough = ns.get('run.passthrough_args') or []
    ns['run.passthrough_args'] = (
        rc_passthrough + dc_run_args + script_passthrough + existing_passthrough
    )

    # 10. Handle run specifics
    if ns.get('subcommand') == 'run':
        _apply_run_specifics(ns, ctx, dc_ns, script_ns, rc_ns)

    # 11. Bridge dc top-level name → run.name (lowest priority fallback).
    #     Skipped when the devcontainer CLI is driving (it manages naming)
    #     or when run.name is already set (CLI --name or customizations.podrun.name).
    if ns.get('dc.name') and not ns.get('run.name') and not ns.get('internal.dc_from_cli'):
        ns['run.name'] = ns['dc.name']

    return ctx


# ---------------------------------------------------------------------------
# Boolean flag normalization
# ---------------------------------------------------------------------------

_BOOL_TRUE = frozenset({'true', '1'})
_BOOL_FALSE = frozenset({'false', '0'})
_BOOL_VALUES = _BOOL_TRUE | _BOOL_FALSE
_BOOL_PT_PREFIX = '--__bool_pt__'


def _normalize_bool_flags(
    argv: List[str], bool_flags: frozenset, short_to_long: Optional[dict] = None
) -> List[str]:
    """Normalize boolean flag value forms for argparse compatibility.

    Podman boolean flags accept ``--flag=true``, ``--flag=false``,
    ``--flag true``, and ``--flag false``.  Argparse registers them with
    ``nargs=0`` which rejects explicit values.

    Explicit-value forms are rewritten to a ``--__bool_pt__flag=value`` variant
    that argparse handles with ``nargs=1`` (same dest, same ordering).
    After parsing, :func:`_strip_pt_bool_flags` converts them back to
    ``--flag=value`` in the passthrough list.

    Short flags with explicit values (``-d=true``) are translated to their
    long form via *short_to_long* (e.g. ``-d=false`` → ``--__bool_pt__detach=false``).

    For short flags, only the equals form is handled; the space form is too
    ambiguous (``-d true`` — is ``true`` the image?).
    """
    stl = short_to_long or {}
    result: List[str] = []
    skip_next = False
    for i, arg in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if '=' in arg:
            name, _, value = arg.partition('=')
            if name in bool_flags and value.lower() in _BOOL_VALUES:
                # Resolve short flag to long form for the __bool_pt__ variant.
                long_name = stl.get(name, name) if not name.startswith('--') else name
                pt_name = _BOOL_PT_PREFIX + long_name.lstrip('-')
                result.append(f'{pt_name}={value}')
                continue
        if arg in bool_flags and arg.startswith('--'):
            # Space form: ``--flag true`` / ``--flag false``
            if i + 1 < len(argv) and argv[i + 1].lower() in _BOOL_VALUES:
                pt_name = _BOOL_PT_PREFIX + arg[2:]
                result.append(f'{pt_name}={argv[i + 1]}')
                skip_next = True
                continue
        result.append(arg)
    return result


def _strip_pt_bool_flags(args: List[str]) -> List[str]:
    """Convert ``--__bool_pt__flag value`` pairs back to ``--flag=value``.

    Called after argparse to restore the original flag names in passthrough
    lists.  Handles both the space form (``['--__bool_pt__flag', 'val']``)
    produced by ``_PassthroughAction`` and any equals form that might
    survive.
    """
    result: List[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith(_BOOL_PT_PREFIX):
            real_name = '--' + arg[len(_BOOL_PT_PREFIX) :]
            if '=' in arg:
                # --__bool_pt__flag=value (shouldn't normally happen after
                # _PassthroughAction, but handle defensively)
                _, _, val = arg.partition('=')
                real_name_base = '--' + arg[len(_BOOL_PT_PREFIX) :].split('=', 1)[0]
                result.append(f'{real_name_base}={val}')
            elif i + 1 < len(args):
                # Space form from _PassthroughAction: --__bool_pt__flag value
                result.append(f'{real_name}={args[i + 1]}')
                i += 2
                continue
            else:
                # Trailing --__bool_pt__flag with no value (shouldn't happen)
                result.append(real_name)
        else:
            result.append(arg)
        i += 1
    return result


# ---------------------------------------------------------------------------
# Custom argparse Actions
# ---------------------------------------------------------------------------


class _PassthroughAction(argparse.Action):
    """Collect podman flag + value into a shared list on the namespace.

    Each invocation appends ``[flag]`` (for boolean) or ``[flag, value]``.
    Argparse calls the action once per flag occurrence, so list-type flags
    like ``-e`` and ``-v`` that appear multiple times are handled naturally.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        items = getattr(namespace, self.dest, None) or []
        items.append(option_string)
        if isinstance(values, list):
            items.extend(values)
        elif values is not None:
            items.append(values)
        setattr(namespace, self.dest, items)


# ---------------------------------------------------------------------------
# Parser builders
# ---------------------------------------------------------------------------


def build_root_parser(flags=None) -> argparse.ArgumentParser:
    """Build the top-level parser with global flags and subcommand routing.

    Global podrun flags use ``dest='root_*'`` prefix.  Podman global flags
    (``--root``, ``--remote``, etc.) are captured via
    :class:`_PassthroughAction` into ``ns.podman_global_args``.

    ``run`` and ``store`` are real subparsers with their own flags;
    other podman subcommands are empty passthrough subparsers.
    """
    if flags is None:
        flags = load_podman_flags()

    parser = argparse.ArgumentParser(
        prog='podrun',
        description='A podman run superset with host identity overlays.',
        add_help=False,
        allow_abbrev=False,
    )
    opts = parser.add_argument_group('Options')

    # -- Podrun global flags (dest='root_*') ----------------------------------
    opts.add_argument(
        '--print-cmd',
        '--dry-run',
        dest='root.print_cmd',
        action='store_true',
        default=None,
        help='Print the podman command instead of executing it',
    )
    opts.add_argument(
        '--devconfig',
        dest='root.config',
        metavar='PATH',
        help='Explicit path to devcontainer.json',
    )
    opts.add_argument(
        '--config-script',
        dest='root.config_script',
        action='append',
        default=None,
        metavar='PATH',
        help='Run Python script and inline its stdout as args (may be repeated)',
    )
    opts.add_argument(
        '--no-devconfig',
        dest='root.no_devconfig',
        action='store_true',
        default=None,
        help='Skip devcontainer.json discovery',
    )
    opts.add_argument(
        '--no-podrunrc',
        dest='root.no_podrunrc',
        action='store_true',
        default=None,
        help='Skip ~/.podrunrc* discovery',
    )
    opts.add_argument(
        '--completion',
        dest='root.completion',
        metavar='SHELL',
        choices=['bash', 'zsh', 'fish'],
        help='Generate shell completion script and exit',
    )
    opts.add_argument(
        '--version',
        '-v',
        dest='root.version',
        action='store_true',
        default=None,
        help=argparse.SUPPRESS,
    )
    opts.add_argument(
        '--cleanup',
        dest='root.cleanup',
        action='append',
        choices=sorted(_CLEANUP_MODES),
        metavar='MODE',
        help='Remove runtime artifacts and exit. MODE: all, staging, cache, stores. Repeatable.',
    )
    opts.add_argument(
        '--__cleanup__',
        dest='root.cleanup',
        action='append_const',
        const='all',
        help=argparse.SUPPRESS,
    )

    # -- Store-related global flags (with translation) ------------------------
    opts.add_argument(
        '--local-store',
        dest='root.local_store',
        metavar='DIR',
        default=None,
        help='Use project-local store directory',
    )
    opts.add_argument(
        '--local-store-ignore',
        dest='root.local_store_ignore',
        action='store_true',
        default=None,
        help='Suppress auto-discovery of project-local store',
    )
    opts.add_argument(
        '--local-store-auto-init',
        dest='root.local_store_auto_init',
        action='store_true',
        default=None,
        help='Auto-create store if missing (requires --local-store)',
    )
    opts.add_argument(
        '--local-store-info',
        dest='root.local_store_info',
        action='store_true',
        default=None,
        help='Print store information and exit',
    )
    opts.add_argument(
        '--local-store-destroy',
        dest='root.local_store_destroy',
        action='store_true',
        default=None,
        help='Remove project-local store before proceeding',
    )

    # -- NFS remediation flags ------------------------------------------------
    opts.add_argument(
        '--nfs-remediate',
        dest='root.nfs_remediate',
        default=None,
        choices=['error', 'init', 'mv', 'rm', 'prompt'],
        metavar='MODE',
        help='NFS storage detection/remediation mode (default: init)',
    )
    opts.add_argument(
        '--nfs-remediate-path',
        dest='root.nfs_remediate_path',
        metavar='DIR',
        default=None,
        help='Base path for NFS-remediated storage (default: /opt/podman-local-storage)',
    )

    # -- Podman global value flags (passthrough) ------------------------------
    for flag in sorted(flags.global_value_flags):
        if flag in _PODRUN_HANDLED_ROOT_FLAGS:
            continue
        opts.add_argument(
            flag,
            action=_PassthroughAction,
            dest='podman_global_args',
            nargs=1,
            help=argparse.SUPPRESS,
        )

    # -- Podman global boolean flags (passthrough) ----------------------------
    for flag in sorted(flags.global_boolean_flags):
        if flag in _PODRUN_HANDLED_ROOT_FLAGS:
            continue
        opts.add_argument(
            flag,
            action=_PassthroughAction,
            dest='podman_global_args',
            nargs=0,
            help=argparse.SUPPRESS,
        )
        # Explicit-value variant: --__bool_pt__flag=value (nargs=1) for
        # --flag=true/false forms rewritten by _normalize_bool_flags.
        if flag.startswith('--'):
            opts.add_argument(
                _BOOL_PT_PREFIX + flag[2:],
                action=_PassthroughAction,
                dest='podman_global_args',
                nargs=1,
                help=argparse.SUPPRESS,
            )

    # -- Subparsers for routing -----------------------------------------------
    subs = parser.add_subparsers(dest='subcommand', title='Available Commands', required=False)

    # Real subparsers for podrun commands (full flag parsing)
    run_parser = _build_run_subparser(subs, flags.run_value_flags, flags.run_boolean_flags)

    # Empty subparsers for podman passthrough commands
    for subcmd in sorted(flags.subcommands - {'run'}):
        subs.add_parser(subcmd, add_help=False)

    # Docker-compat aliases that podman accepts but omits from ``podman --help``
    for alias in sorted(_DOCKER_COMPAT_SUBCOMMANDS - flags.subcommands):
        subs.add_parser(alias, add_help=False)

    # Stash for help/completion access
    parser._run_subparser = run_parser  # type: ignore[attr-defined]

    return parser


def _build_run_subparser(subs, run_value_flags, run_boolean_flags) -> argparse.ArgumentParser:
    """Add ``run`` subparser with podrun run flags and podman value flag passthrough.

    Podrun run flags use ``dest='run_*'`` prefix.
    Podman run value flags are collected via :class:`_PassthroughAction`
    into ``ns.run.passthrough_args``.
    """
    parser = subs.add_parser(
        'run',
        add_help=False,
        allow_abbrev=False,
        description='Additional run options for host identity overlays.',
    )
    opts = parser.add_argument_group('Options')

    # -- Podrun run flags (dest='run_*') --------------------------------------
    opts.add_argument('--name', dest='run.name', metavar='NAME', help=argparse.SUPPRESS)
    opts.add_argument(
        '--label',
        '-l',
        dest='run.label',
        action='append',
        default=None,
        metavar='KEY=VALUE',
        help=argparse.SUPPRESS,
    )
    opts.add_argument(
        '--user-overlay',
        dest='run.user_overlay',
        action='store_true',
        default=None,
        help='Map host user identity into container',
    )
    opts.add_argument(
        '--host-overlay',
        dest='run.host_overlay',
        action='store_true',
        default=None,
        help='Overlay host system context (implies --user-overlay)',
    )
    opts.add_argument(
        '--interactive-overlay',
        dest='run.interactive_overlay',
        action='store_true',
        default=None,
        help='Interactive overlay (-it, --detach-keys)',
    )
    opts.add_argument(
        '--session',
        dest='run.session',
        action='store_true',
        default=None,
        help='Session overlay (implies --host-overlay + --interactive-overlay)',
    )
    opts.add_argument(
        '--adhoc',
        dest='run.adhoc',
        action='store_true',
        default=None,
        help='Ad-hoc overlay (implies --session + --rm)',
    )
    opts.add_argument(
        '--dot-files-overlay',
        '--dotfiles',
        dest='run.dot_files_overlay',
        action='store_true',
        default=None,
        help='Mount host dotfiles into container (implies --user-overlay)',
    )
    opts.add_argument(
        '--no-auto-resolve-git-submodules',
        dest='run.no_auto_resolve_git_submodules',
        action='store_true',
        default=None,
        help='Disable automatic git submodule resolution and mounting',
    )
    opts.add_argument(
        '--print-overlays',
        dest='run.print_overlays',
        action='store_true',
        default=None,
        help='Print each overlay group and its settings, then exit',
    )
    opts.add_argument(
        '--x11',
        dest='run.x11',
        action='store_true',
        default=None,
        help='Enable X11 forwarding',
    )
    opts.add_argument(
        '--podman-remote',
        dest='run.podman_remote',
        action='store_true',
        default=None,
        help='Podman socket passthrough',
    )
    opts.add_argument(
        '--shell', dest='run.shell', metavar='SHELL', help='Shell to use inside container'
    )

    login_group = opts.add_mutually_exclusive_group()
    login_group.add_argument(
        '--login',
        action='store_const',
        const=True,
        default=None,
        dest='run.login',
        help='Run shell as login shell',
    )
    login_group.add_argument(
        '--no-login',
        action='store_const',
        const=False,
        dest='run.login',
        help='Disable login shell',
    )

    opts.add_argument(
        '--prompt-banner', dest='run.prompt_banner', metavar='TEXT', help='Prompt banner text'
    )
    opts.add_argument(
        '--auto-attach',
        dest='run.auto_attach',
        action='store_true',
        default=None,
        help='Auto attach to named container if already running',
    )
    opts.add_argument(
        '--auto-replace',
        dest='run.auto_replace',
        action='store_true',
        default=None,
        help='Auto replace named container if already running',
    )
    opts.add_argument(
        '--export',
        dest='run.export',
        action='append',
        default=None,
        metavar='SRC:DST[:0]',
        help='Export container path to host. May be repeated.',
    )
    opts.add_argument(
        '--fuse-overlayfs',
        dest='run.fuse_overlayfs',
        action='store_true',
        default=None,
        help='Use fuse-overlayfs for overlay mounts',
    )

    # -- Podman run value flags (passthrough, dest='run.passthrough_args') ----
    for flag in sorted(run_value_flags):
        if flag in _PODRUN_HANDLED_RUN_FLAGS:
            continue
        opts.add_argument(
            flag,
            action=_PassthroughAction,
            dest='run.passthrough_args',
            nargs=1,
            help=argparse.SUPPRESS,
        )

    # -- Podman run boolean flags (passthrough, dest='run.passthrough_args') --
    for flag in sorted(run_boolean_flags):
        opts.add_argument(
            flag,
            action=_PassthroughAction,
            dest='run.passthrough_args',
            nargs=0,
            help=argparse.SUPPRESS,
        )
        # Explicit-value variant: --__bool_pt__flag=value (nargs=1) for
        # --flag=true/false forms rewritten by _normalize_bool_flags.
        if flag.startswith('--'):
            opts.add_argument(
                _BOOL_PT_PREFIX + flag[2:],
                action=_PassthroughAction,
                dest='run.passthrough_args',
                nargs=1,
                help=argparse.SUPPRESS,
            )

    # -- IMAGE [COMMAND [ARG...]] boundary ------------------------------------
    # REMAINDER stops flag parsing at the first positional so that command
    # args like ``bash -c echo`` are not consumed as podman flags.
    parser.add_argument('run.trailing', nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    return parser  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# PodrunContext
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class PodrunContext:
    """Structured context object for podrun.

    Returned by :func:`parse_args` and threaded through the call chain.
    Access parsed values through the dict using prefix conventions::

        ctx.ns['subcommand']              # 'run', 'store', 'ps', etc. or None
        ctx.ns['root.print_cmd']          # global podrun config flags
        ctx.ns.get('podman_global_args') or []  # ['--root', '/x', '--remote', ...]

    Runtime state (``podman_path``, ``dc_from_cli``) is set after parsing
    by ``main()`` and ``resolve_config()`` respectively, eliminating the
    need to thread ``podman_path`` as a separate parameter.
    """

    ns: dict
    trailing_args: List[str]  # For run: image + command
    explicit_command: List[str]  # Args after '--'
    raw_argv: List[str]  # Original argv
    subcmd_passthrough_args: List[str]  # For passthrough subcommands
    podman_path: str = 'podman'  # Resolved podman binary path
    dc_from_cli: bool = False  # True when devcontainer CLI is driving
    copy_staging: Optional[List[tuple]] = None  # Copy-staging items from :O fallback


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------


def parse_args(argv: List[str], flags=None) -> PodrunContext:
    """Parse podrun CLI arguments and return a structured :class:`PodrunContext`.

    Architecture:

    1. Split on ``--`` separator.
    2. Root parser (``build_root_parser``) consumes global podrun flags,
       podman global flags, and routes to real subparsers for ``run``/``store``
       or empty subparsers for other podman subcommands.
    3. For ``run``: the REMAINDER positional stops flag parsing at the image
       boundary so command args (e.g. ``-c``) are not consumed as podman flags.
    4. For ``store``: unknowns are rejected (strict parsing).
    5. For other subcommands: unknowns are forwarded as passthrough.
    6. No subcommand: for immediate-exit flags (``--version``, ``--help``, etc.).
    """
    raw_argv = list(argv)

    # Split on '--' separator
    if '--' in argv:
        idx = argv.index('--')
        flag_section, explicit_command = argv[:idx], argv[idx + 1 :]
    else:
        flag_section, explicit_command = argv, []

    # Resolve flags early so we can normalize boolean flag forms before parsing.
    if flags is None:
        flags = load_podman_flags()

    # Normalize --bool=true/false and --bool true/false before argparse sees
    # them.  Explicit-value forms are rewritten to --__bool_pt__flag=value variants
    # that argparse handles via nargs=1.  _strip_pt_bool_flags() restores
    # them after parsing so downstream code sees clean flag names.
    all_bool_flags = flags.global_boolean_flags | flags.run_boolean_flags
    flag_section = _normalize_bool_flags(flag_section, all_bool_flags, flags.bool_short_to_long)

    # Single-pass parse: root parser handles global flags + subcommand routing;
    # real subparsers (run/store) handle subcommand-specific flags.
    root = build_root_parser(flags)
    ns_raw, unknowns = root.parse_known_args(flag_section)
    ns = vars(ns_raw)

    # Strip __bool_pt__ prefix from passthrough — restores --flag=value form
    # in the original argv order.
    if ns.get('run.passthrough_args'):
        ns['run.passthrough_args'] = _strip_pt_bool_flags(ns['run.passthrough_args'])
    if ns.get('podman_global_args'):
        ns['podman_global_args'] = _strip_pt_bool_flags(ns['podman_global_args'])

    subcmd = ns['subcommand']
    trailing_args = []
    subcmd_passthrough_args = []

    if subcmd == 'run':
        # REMAINDER captured everything from the image onward (IMAGE + COMMAND).
        # Any unknowns are flags the scrape didn't cover; prepend them so
        # they're still forwarded to podman.
        run_trailing = ns.pop('run.trailing', None) or []
        trailing_args = list(unknowns) + run_trailing

    elif subcmd is not None:
        # Passthrough subcommand: unknowns are the raw args after the
        # subcommand token.
        subcmd_passthrough_args = list(unknowns)

    # subcmd is None: handled in main() for --version, --help, etc.

    return PodrunContext(
        ns=ns,
        trailing_args=trailing_args,
        explicit_command=explicit_command,
        raw_argv=raw_argv,
        subcmd_passthrough_args=subcmd_passthrough_args,
    )


# ---------------------------------------------------------------------------
# Container state
# ---------------------------------------------------------------------------


def detect_container_state(
    name: str,
    global_flags=None,
    podman_path: str = 'podman',
):
    """Returns ``"running"``, ``"stopped"``, or ``None``."""
    if not name:
        return None
    gf = ' '.join(_shell_quote(f) for f in global_flags) + ' ' if global_flags else ''
    fmt = _shell_quote('{{.State.Status}}')
    result = run_os_cmd(
        f'{_shell_quote(podman_path)} {gf}inspect --format={fmt} {_shell_quote(name)}'
    )
    if result.returncode != 0:
        return None
    status = result.stdout.strip()
    if status == 'running':
        return 'running'
    if status in ('created', 'exited', 'stopped', 'dead', 'paused'):
        return 'stopped'
    return None


def _handle_running_state(auto_attach, auto_replace, is_interactive):
    """Decide action for a running container: attach, replace, or None."""
    if auto_attach:
        return 'attach'
    if auto_replace:
        return 'replace'
    if auto_attach is False and auto_replace is False:
        return None
    if yes_no_prompt('Attach to already running instance?', True, is_interactive):
        return 'attach'
    if yes_no_prompt('Replace already running instance?', False, is_interactive):
        return 'replace'
    return None


def _handle_stopped_state(name, auto_attach, auto_replace, is_interactive):
    """Decide action for a stopped container: restart, replace, or None."""
    if auto_attach:
        return 'restart'
    if auto_replace:
        return 'replace'
    if auto_attach is False and auto_replace is False and not is_interactive:
        return None
    if yes_no_prompt('Restart stopped instance?', True, is_interactive):
        return 'restart'
    if yes_no_prompt('Replace stopped instance?', False, is_interactive):
        return 'replace'
    return None


def handle_container_state(ctx: 'PodrunContext', global_flags=None):
    """Returns ``"run"``, ``"attach"``, ``"restart"``, ``"replace"``, or ``None`` (exit).

    Reads from *ctx.ns*: ``run.name``, ``run.auto_attach``, ``run.auto_replace``.
    """
    ns = ctx.ns
    name = ns.get('run.name')
    if not name:
        return 'run'

    state = detect_container_state(name, global_flags=global_flags, podman_path=ctx.podman_path)
    if state is None:
        return 'run'

    is_interactive = sys.stdin.isatty()
    auto_attach = ns.get('run.auto_attach')
    auto_replace = ns.get('run.auto_replace')

    if state == 'running':
        return _handle_running_state(auto_attach, auto_replace, is_interactive)
    return _handle_stopped_state(name, auto_attach, auto_replace, is_interactive)


def query_container_info(
    name: str,
    global_flags=None,
    podman_path: str = 'podman',
) -> Tuple[str, str]:
    """Read PODRUN_WORKDIR and PODRUN_OVERLAYS from container env via inspect.

    Returns ``(workdir, overlays)`` where each is ``''`` if not found.
    """
    gf = ' '.join(_shell_quote(f) for f in global_flags) + ' ' if global_flags else ''
    fmt = _shell_quote('{{range .Config.Env}}{{println .}}{{end}}')
    result = run_os_cmd(
        f'{_shell_quote(podman_path)} {gf}inspect --format={fmt} {_shell_quote(name)}'
    )
    workdir = ''
    overlays = ''
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if line.startswith(f'{ENV_PODRUN_WORKDIR}='):
                workdir = line.split('=', 1)[1]
            elif line.startswith(f'{ENV_PODRUN_OVERLAYS}='):
                overlays = line.split('=', 1)[1]
    return workdir, overlays


def build_podman_exec_args(
    ns,
    name: str,
    container_workdir: str = '',
    trailing_args=None,
    explicit_command=None,
) -> List[str]:
    """Build ``podman exec`` args for attaching to a running container.

    Shell/login handling is delegated to exec-entrypoint.sh inside the
    container.  CLI overrides are passed as ``-e=PODRUN_*`` env vars.
    """
    args = ['exec']
    args.append('-it')
    args.append('--detach-keys=ctrl-q,ctrl-q')

    if container_workdir:
        args.append(f'-w={container_workdir}')

    try:
        cols, rows = shutil.get_terminal_size()
        args.append(f'-e={ENV_PODRUN_STTY_INIT}=rows {rows} cols {cols}')
    except (ValueError, OSError):
        pass

    args.append(f'-e=ENV={PODRUN_RC_PATH}')

    if ns.get('run.shell'):
        args.append(f'-e={ENV_PODRUN_SHELL}={ns["run.shell"]}')
    if ns.get('run.login') is not None:
        args.append(f'-e={ENV_PODRUN_LOGIN}={"1" if ns["run.login"] else "0"}')

    args.append(name)

    # Determine command: explicit_command ('--' args) > trailing_args after image > interactive
    command = explicit_command or (
        trailing_args[1:] if trailing_args and len(trailing_args) > 1 else []
    )
    if command:
        args.extend(command)
    else:
        args.append(PODRUN_EXEC_ENTRY_PATH)

    return args


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------


def build_run_command(ctx: PodrunContext) -> List[str]:
    """Build the full ``podman run`` command from a PodrunContext."""
    ns = ctx.ns
    cmd = [ctx.podman_path]
    cmd.extend(ns.get('podman_global_args') or [])
    cmd.append('run')

    # Named podrun run flags that map to podman flags
    if ns.get('run.name'):
        cmd.append(f'--name={ns["run.name"]}')
    for lbl in ns.get('run.label') or []:
        cmd.append(f'--label={lbl}')

    # Passthrough args (podman value + boolean flags)
    cmd.extend(ns.get('run.passthrough_args') or [])

    # Trailing positionals (image + command)
    cmd.extend(ctx.trailing_args)

    # Explicit command after '--'
    if ctx.explicit_command:
        cmd.append('--')
        cmd.extend(ctx.explicit_command)

    return cmd


def build_overlay_run_command(ctx: PodrunContext) -> Tuple[List[str], List[str]]:  # noqa: C901
    """Generate entrypoints, build overlay args, and return the full run command.

    Returns ``(cmd, caps_to_drop)`` where *cmd* is the complete
    ``podman run ...`` arg list and *caps_to_drop* is the list of
    capabilities the entrypoint should drop after bootstrap.

    Overlay args are injected into ``ns['run.passthrough_args']`` before
    delegating to :func:`build_run_command`.
    """
    ns = ctx.ns
    pt = ns.get('run.passthrough_args') or []
    overlay_args = []
    caps_to_drop = []

    # Validate overlay combinations
    _validate_overlay_args(ns)

    # Alt-entrypoint extraction — when user-overlay is active, extract any
    # --entrypoint from passthrough so it doesn't override the podrun entrypoint.
    alt_entrypoint = None
    if ns.get('run.user_overlay'):
        alt_entrypoint, pt = _extract_passthrough_entrypoint(pt)

    # --user extraction — when user-overlay is active, extract any --user/-u
    # from passthrough, validate it matches the host identity, and let
    # _user_overlay_args inject the canonical --user={UID}:{GID}.
    if ns.get('run.user_overlay'):
        user_value, pt = _extract_passthrough_user(pt)
        if user_value is not None:
            _validate_passthrough_user(user_value)

    # Generate entrypoints and build user overlay args
    if ns.get('run.user_overlay'):
        entrypoint_path = generate_run_entrypoint(ns, caps_to_drop=compute_caps_to_drop(pt))
        rc_path = generate_rc_sh(ns)
        exec_entry_path = generate_exec_entrypoint(ns)
        # Store for config sidecar (entrypoint linkage)
        ns['internal.entrypoint_path'] = entrypoint_path
        ns['internal.rc_path'] = rc_path
        ns['internal.exec_entry_path'] = exec_entry_path
        user_args, caps_to_drop = _user_overlay_args(
            ns, pt, entrypoint_path, rc_path, exec_entry_path
        )
        overlay_args.extend(user_args)
        if alt_entrypoint:
            overlay_args.append(f'--env={ENV_PODRUN_ALT_ENTRYPOINT}={alt_entrypoint}')

    if ns.get('run.interactive_overlay'):
        overlay_args.extend(_interactive_overlay_args(ns, pt))
    if ns.get('run.host_overlay'):
        overlay_args.extend(_host_overlay_args(ns, pt))
    if ns.get('run.dot_files_overlay'):
        overlay_args.extend(_dot_files_overlay_args(ns, pt))
    if ns.get('run.x11'):
        overlay_args.extend(_x11_args(ns))
    if ns.get('run.podman_remote'):
        overlay_args.extend(_podman_remote_args(ns))
    if ns.get('run.adhoc'):
        if not _passthrough_has_exact(pt, '--rm'):
            overlay_args.append('--rm')

    overlay_args.extend(_env_args(ns))

    # Propagate the staging directory when podman-remote is active so nested
    # podrun can write files visible to the host daemon.  Use _daemon_dir()
    # (not PODRUN_TMP) as the mount source so multi-level nesting forwards
    # the original host path.
    if ns.get('run.podman_remote'):
        daemon = _daemon_dir()
        overlay_args.append(f'-v={daemon}:{PODRUN_HOST_TMP_MOUNT}:z')
        overlay_args.append(f'--env={ENV_PODRUN_HOST_TMP}={daemon}')

    # Single-pass volume processing: tilde expansion, :0 extraction,
    # manifest source translation, and mount map building.
    expand = bool(ns.get('run.user_overlay'))
    nested = _staging_dir() != _daemon_dir()
    manifest_mounts = _read_mount_manifest().get('mounts', {}) if nested else None
    copy_staging = ctx.copy_staging or []

    # :O (overlay) items were already resolved by _resolve_overlay_mounts().
    overlay_args, extra_cs, mount_map = _process_volume_args(
        overlay_args,
        expand_tilde=expand,
        manifest_mounts=manifest_mounts,
    )
    copy_staging.extend(extra_cs)
    pt, extra_cs, pt_mm = _process_volume_args(
        pt,
        expand_tilde=expand,
        manifest_mounts=manifest_mounts,
    )
    copy_staging.extend(extra_cs)
    mount_map.update(pt_mm)

    # In nested-remote mode, translate copy-staging host paths using the
    # manifest's copy_staging section (these are mount sources that don't
    # appear as mount destinations in the mounts section).
    if nested and copy_staging:
        cs_map = _read_mount_manifest().get('copy_staging', {})
        copy_staging = [(cs_map.get(cp, hp), cp) for hp, cp in copy_staging]

    if copy_staging:
        staging_args = _copy_staging_args(copy_staging, _DOTFILES_CHMOD)
        staging_args, _, staging_mm = _process_volume_args(
            staging_args,
            manifest_mounts=manifest_mounts,
        )
        overlay_args.extend(staging_args)
        mount_map.update(staging_mm)

    # Inject overlay args into passthrough
    ns['run.passthrough_args'] = overlay_args + pt

    # Write the mount manifest so nested podrun can resolve daemon-visible
    # source paths for each -v mount.
    if ns.get('run.podman_remote'):
        _write_mount_manifest(mount_map, copy_staging)

    return build_run_command(ctx), caps_to_drop


def build_passthrough_command(ctx: PodrunContext) -> List[str]:
    """Build a passthrough ``podman <subcommand> ...`` command."""
    ns = ctx.ns
    cmd = [ctx.podman_path]
    cmd.extend(ns.get('podman_global_args') or [])
    cmd.append(ns['subcommand'])
    # Translate -u root → -u 0.  With --userns=keep-id, podman resolves
    # usernames by reading /etc/passwd from outside the container's mount
    # namespace, which may not see entrypoint modifications.  Numeric UIDs
    # bypass the lookup entirely.
    args = list(ctx.subcmd_passthrough_args)
    for i, arg in enumerate(args):
        if arg in ('-u', '--user') and i + 1 < len(args) and args[i + 1] == 'root':
            args[i + 1] = '0'
        elif arg in ('-u=root', '--user=root'):
            args[i] = arg.replace('root', '0', 1)
    cmd.extend(args)
    if ctx.explicit_command:
        cmd.append('--')
        cmd.extend(ctx.explicit_command)
    return cmd


# ---------------------------------------------------------------------------
# Help system
# ---------------------------------------------------------------------------


def print_help(subcmd, argv, podman_path):
    """Print context-appropriate help if -h/--help appears before '--', then exit.

    Returns without action when no help flag is found or the subcommand
    is not handled by podrun (store uses argparse built-in help; other
    podman subcommands are passed through).
    """
    if subcmd not in (None, 'run'):
        return

    sep_idx = argv.index('--') if '--' in argv else len(argv)
    if not any(a in ('-h', '--help') for a in argv[:sep_idx]):
        return

    if subcmd == 'run':
        podman_cmd = f'{_shell_quote(podman_path)} run --help'
        replace_from, replace_to = 'podman run', 'podrun run'
        podrun_parser = build_root_parser()._run_subparser  # type: ignore[attr-defined]
    else:
        podman_cmd = f'{_shell_quote(podman_path)} --help'
        replace_from, replace_to = 'podman', 'podrun'
        podrun_parser = build_root_parser()

    result = run_os_cmd(podman_cmd)
    if result.returncode == 0:
        print(result.stdout.rstrip().replace(replace_from, replace_to))

    podrun_help = podrun_parser.format_help()
    # Drop the usage line (first paragraph), keep description + options
    sections = podrun_help.split('\n\n', 1)
    body = sections[-1] if len(sections) > 1 else podrun_help
    print()
    print('Podrun:')
    print()
    print(body)

    sys.exit(0)


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------


def print_version(podman_path=None):
    """Print podman and podrun versions."""
    if podman_path is None:
        podman_path = _default_podman_path() or 'podman'
    result = run_os_cmd(f'{_shell_quote(podman_path)} --version')
    if result.returncode == 0:
        print(result.stdout.strip())
    print(f'podrun version {__version__}')


# ---------------------------------------------------------------------------
# Podman help scraper
# ---------------------------------------------------------------------------


def _scrape_podman_help(podman_path, subcmd=None):  # noqa: C901
    """Scrape ``podman [subcmd] --help`` and return (value_flags, bool_flags, subcommands).

    *value_flags*: flags that take an argument (e.g. ``--env``, ``-e``).
    *bool_flags*: flags with no argument (e.g. ``--rm``, ``--help``).
    *subcommands*: subcommand names from the "Available Commands" section.

    Returns ``None`` on failure.
    """
    cmd_parts = [_shell_quote(podman_path)]
    if subcmd:
        cmd_parts.append(subcmd)
    cmd_parts.append('--help')
    result = run_os_cmd(' '.join(cmd_parts))
    if result.returncode != 0:
        return None

    value_flags = set()
    bool_flags = set()
    subcommands = set()
    bool_short_to_long: dict = {}
    in_commands = False

    for line in result.stdout.splitlines():
        # Subcommand section: "Available Commands:" followed by "  name  description"
        if line.strip().startswith('Available Commands'):
            in_commands = True
            continue
        if in_commands:
            if not line.strip():
                in_commands = False
                continue
            m = re.match(r'\s+(\S+)\s', line)
            if m:
                subcommands.add(m.group(1))
            continue

        # Flag lines: optional short flag, long flag, optional type, help text
        m = re.match(
            r'\s*(?P<short>-\w)?,?\s*(?P<long>--[^\s]+)'
            r'\s+(?P<val_type>[^\s]+)?\s{2,}(?P<help>\w+.*)',
            line,
        )
        if m:
            is_bool = not m.group('val_type')
            bucket = bool_flags if is_bool else value_flags
            bucket.add(m.group('long'))
            if m.group('short'):
                bucket.add(m.group('short'))
                if is_bool:
                    bool_short_to_long[m.group('short')] = m.group('long')

    return value_flags, bool_flags, subcommands, bool_short_to_long


# ---------------------------------------------------------------------------
# Completion generators
# ---------------------------------------------------------------------------


def _completion_data(flags: Optional[PodmanFlags] = None) -> dict:
    """Build completion metadata by introspecting argparse parsers.

    Returns a dict with:
    - ``flags_str`` — space-joined list of all podrun-specific flags
    - ``value_flags_str`` — subset that take values
    - ``subcmds_str`` — empty string (no podrun subcommands)
    """
    if flags is None:
        flags = load_podman_flags()
    parser = build_root_parser(flags)
    run_parser = parser._run_subparser  # type: ignore[attr-defined]

    all_flags = []
    value_flags = []
    _SKIP_ACTIONS = (argparse._HelpAction, _PassthroughAction)
    _BOOL_ACTIONS = ('store_true', 'store_false', 'store_const')

    for p in (parser, run_parser):
        for action in p._actions:
            if isinstance(action, _SKIP_ACTIONS):
                continue
            # Only podrun-specific flags (root.* or run.* dest)
            dest = getattr(action, 'dest', '')
            if not (dest.startswith('root.') or dest.startswith('run.')):
                continue
            for opt in action.option_strings:
                all_flags.append(opt)
                # Classify: value flag if it's not a boolean-style action
                action_name = getattr(action, 'action', None)
                if action_name is None:
                    # Real Action subclass — check the class name
                    cls_name = type(action).__name__
                    if cls_name not in (
                        '_StoreTrueAction',
                        '_StoreFalseAction',
                        '_StoreConstAction',
                    ):
                        value_flags.append(opt)
                elif action_name not in _BOOL_ACTIONS:
                    value_flags.append(opt)

    return {
        'flags_str': ' '.join(sorted(all_flags)),
        'value_flags_str': ' '.join(sorted(value_flags)),
        'subcmds_str': '',
    }


def _generate_bash_completion() -> str:
    """Return a bash completion script that wraps podman's Cobra completions."""
    cd = _completion_data()
    flags_str = cd['flags_str']
    value_flags_str = cd['value_flags_str']

    return textwrap.dedent(f"""\
        _podrun() {{
            local cur="${{COMP_WORDS[COMP_CWORD]}}"
            local podrun_flags="{flags_str}"
            local podrun_value_flags="{value_flags_str}"

            # Build filtered args for podman, stripping podrun-only flags
            local args=()
            local has_subcmd=false
            local i=1
            while [ $i -lt $COMP_CWORD ]; do
                local word="${{COMP_WORDS[$i]}}"
                local flag_name="${{word%%=*}}"
                # Check if this is a podrun-only flag
                local is_podrun=false
                for pf in $podrun_flags; do
                    if [ "$flag_name" = "$pf" ]; then
                        is_podrun=true
                        break
                    fi
                done
                if $is_podrun; then
                    # Skip value for podrun value flags (space-separated form)
                    if [[ "$word" != *=* ]]; then
                        for vf in $podrun_value_flags; do
                            if [ "$word" = "$vf" ]; then
                                i=$((i + 1))
                                break
                            fi
                        done
                    fi
                else
                    args+=("$word")
                    # Check if this is a subcommand (first non-flag arg)
                    if [[ "$word" != -* ]] && ! $has_subcmd; then
                        has_subcmd=true
                    fi
                fi
                i=$((i + 1))
            done

            # Inject 'run' if no subcommand detected
            if ! $has_subcmd; then
                args=("run" "${{args[@]}}")
            fi

            # Call podman __completeNoDesc
            local completions
            completions=$(podman __completeNoDesc "${{args[@]}}" "$cur" 2>/dev/null)

            local directive=0
            local results=()
            while IFS= read -r line; do
                if [[ "$line" == :* ]]; then
                    directive="${{line#:}}"
                else
                    if [ -n "$line" ]; then
                        results+=("$line")
                    fi
                fi
            done <<< "$completions"

            # Merge podrun flags when completing flags
            if [[ "$cur" == -* ]]; then
                for pf in $podrun_flags; do
                    results+=("$pf")
                done
            fi

            mapfile -t COMPREPLY < <(compgen -W "${{results[*]}}" -- "$cur")

            # Handle Cobra directives
            if (( (directive & 2) != 0 )); then
                compopt -o nospace
            fi
            if (( (directive & 4) != 0 )) && [[ "$cur" != -* || ${{#COMPREPLY[@]}} -gt 0 ]]; then
                compopt +o default
            fi
        }}
        complete -o default -F _podrun podrun
    """)


def _generate_zsh_completion() -> str:
    """Return a zsh completion script that wraps podman's Cobra completions."""
    cd = _completion_data()
    flags_str = cd['flags_str']
    value_flags_str = cd['value_flags_str']

    return textwrap.dedent(f"""\
        #compdef podrun

        _podrun() {{
            local podrun_flags=({flags_str})
            local podrun_value_flags=({value_flags_str})

            # Build filtered args for podman, stripping podrun-only flags
            local args=()
            local has_subcmd=false
            local i=2
            while (( i < CURRENT )); do
                local word="${{words[$i]}}"
                local flag_name="${{word%%=*}}"
                local is_podrun=false
                for pf in "${{podrun_flags[@]}}"; do
                    if [[ "$flag_name" = "$pf" ]]; then
                        is_podrun=true
                        break
                    fi
                done
                if $is_podrun; then
                    if [[ "$word" != *=* ]]; then
                        for vf in "${{podrun_value_flags[@]}}"; do
                            if [[ "$word" = "$vf" ]]; then
                                (( i++ ))
                                break
                            fi
                        done
                    fi
                else
                    args+=("$word")
                    if [[ "$word" != -* ]] && ! $has_subcmd; then
                        has_subcmd=true
                    fi
                fi
                (( i++ ))
            done

            if ! $has_subcmd; then
                args=("run" "${{args[@]}}")
            fi

            local cur="${{words[$CURRENT]}}"
            local completions
            completions=$(podman __complete "${{args[@]}}" "$cur" 2>/dev/null)

            local directive=0
            local -a results
            local -a descriptions
            while IFS=$'\\t' read -r comp desc; do
                if [[ "$comp" == :* ]]; then
                    directive="${{comp#:}}"
                else
                    if [[ -n "$comp" ]]; then
                        if [[ -n "$desc" ]]; then
                            results+=("$comp")
                            descriptions+=("$comp:$desc")
                        else
                            results+=("$comp")
                            descriptions+=("$comp")
                        fi
                    fi
                fi
            done <<< "$completions"

            # Merge podrun flags when completing flags
            if [[ "$cur" == -* ]]; then
                for pf in "${{podrun_flags[@]}}"; do
                    results+=("$pf")
                    descriptions+=("$pf:podrun option")
                done
            fi

            _describe 'completions' descriptions

            if (( (directive & 2) != 0 )); then
                compstate[insert]=unambiguous
            fi
        }}

        compdef _podrun podrun
    """)


def _generate_fish_completion() -> str:
    """Return a fish completion script that wraps podman's Cobra completions."""
    cd = _completion_data()
    flags_str = cd['flags_str']
    value_flags_str = cd['value_flags_str']

    return textwrap.dedent(f"""\
        function __podrun_complete
            set -l cmdline (commandline -opc)
            set -l cur (commandline -ct)

            set -l podrun_flags {flags_str}
            set -l podrun_value_flags {value_flags_str}

            # Build filtered args for podman, stripping podrun-only flags
            set -l args
            set -l has_subcmd false
            set -l skip_next false
            for i in (seq 2 (count $cmdline))
                if test "$skip_next" = true
                    set skip_next false
                    continue
                end
                set -l word $cmdline[$i]
                set -l flag_name (string split -m1 '=' -- $word)[1]
                set -l is_podrun false
                for pf in $podrun_flags
                    if test "$flag_name" = "$pf"
                        set is_podrun true
                        break
                    end
                end
                if test "$is_podrun" = true
                    if not string match -q '*=*' -- $word
                        for vf in $podrun_value_flags
                            if test "$word" = "$vf"
                                set skip_next true
                                break
                            end
                        end
                    end
                else
                    set -a args $word
                    if not string match -q '-*' -- $word; and test "$has_subcmd" = false
                        set has_subcmd true
                    end
                end
            end

            if test "$has_subcmd" = false
                set args run $args
            end

            # Call podman __complete
            set -l completions (podman __complete $args "$cur" 2>/dev/null)
            for line in $completions
                if string match -qr '^:' -- $line
                    continue
                end
                if test -n "$line"
                    echo $line
                end
            end

            # Merge podrun flags when completing flags
            if string match -q '-*' -- $cur
                for pf in $podrun_flags
                    echo -e "$pf\\tpodrun option"
                end
            end
        end

        complete -c podrun -f -a '(__podrun_complete)'
    """)


def print_completion(shell: str) -> None:
    """Print shell completion script and exit."""
    generators = {
        'bash': _generate_bash_completion,
        'zsh': _generate_zsh_completion,
        'fish': _generate_fish_completion,
    }
    print(generators[shell]())
    sys.exit(0)


# ---------------------------------------------------------------------------
# Overlay mount resolution (fuse-overlayfs + :O fallback)
# ---------------------------------------------------------------------------


def _resolve_overlay_mounts(ctx: 'PodrunContext'):  # noqa: C901
    """Handle ``--fuse-overlayfs`` storage-opt and ``:O`` mount fallback.

    1. When ``--fuse-overlayfs`` is set, injects ``--storage-opt`` for the
       fuse-overlayfs mount program (unchanged from the old behaviour).
    2. Scans passthrough args for ``:O`` volume mounts and applies fallback
       logic: **file** mounts always get rewritten to copy-staging (overlay
       does not work on individual files); **directory** mounts use native
       ``:O`` when fuse-overlayfs is available, otherwise fall back to
       copy-staging.

    Rewritten items are collected in ``ctx.copy_staging`` for
    consumption by ``build_overlay_run_command()``.

    Must be called **before** ``build_overlay_run_command()`` so that the
    storage-opt is included in the built command and the ``:O`` conversion
    applies to passthrough args before overlay args are prepended.
    """
    ns = ctx.ns
    fuse_path = shutil.which('fuse-overlayfs')

    # Step 1: --fuse-overlayfs → inject storage-opt
    if ns.get('run.fuse_overlayfs'):
        if not fuse_path:
            print(
                'Error: --fuse-overlayfs requested but fuse-overlayfs not found in PATH',
                file=sys.stderr,
            )
            sys.exit(1)
        existing = ns.get('podman_global_args') or []
        ns['podman_global_args'] = existing + [
            '--storage-opt',
            f'overlay.mount_program={fuse_path}',
        ]

    # Step 2: scan for :O and :0 mounts and apply fallback
    #   :0 — always copy-staging (explicit writable-copy request)
    #   :O — copy-staging for files (overlay doesn't work on files) or
    #         dirs without fuse-overlayfs; native overlay otherwise
    pt = ns.get('run.passthrough_args') or []
    result: list = []
    copy_staging_items: list = []
    skip = False
    for i, arg in enumerate(pt):
        if skip:
            skip = False
            continue

        rewritten = False

        # Equals form: -v=/host:/ctr:O or -v=/host:/ctr:0
        m = re.match(r'^(-v=|--volume=)(.+)$', arg)
        if m:
            spec = m.group(2)
            parts = _split_path_colon(spec)
            if len(parts) >= 3 and parts[-1] in ('O', '0'):
                host_path = parts[0]
                container_path = parts[1]
                mode = parts[-1]
                if mode == '0':
                    copy_staging_items.append((host_path, container_path))
                    rewritten = True
                elif os.path.isfile(host_path):
                    copy_staging_items.append((host_path, container_path))
                    rewritten = True
                elif os.path.isdir(host_path) and not fuse_path:
                    copy_staging_items.append((host_path, container_path))
                    rewritten = True
            if not rewritten:
                result.append(arg)
            continue

        # Space form: -v /host:/ctr:O or -v /host:/ctr:0
        if arg in ('-v', '--volume') and i + 1 < len(pt):
            spec = pt[i + 1]
            parts = _split_path_colon(spec)
            if len(parts) >= 3 and parts[-1] in ('O', '0'):
                host_path = parts[0]
                container_path = parts[1]
                mode = parts[-1]
                if mode == '0':
                    copy_staging_items.append((host_path, container_path))
                    rewritten = True
                    skip = True
                elif os.path.isfile(host_path):
                    copy_staging_items.append((host_path, container_path))
                    rewritten = True
                    skip = True
                elif os.path.isdir(host_path) and not fuse_path:
                    copy_staging_items.append((host_path, container_path))
                    rewritten = True
                    skip = True
            if not rewritten:
                result.append(arg)
            continue

        result.append(arg)

    ns['run.passthrough_args'] = result
    ctx.copy_staging = copy_staging_items


# ---------------------------------------------------------------------------
# Run handler
# ---------------------------------------------------------------------------


def _exec_attach(ctx: 'PodrunContext', global_flags):
    """Handle the 'attach' action — exec into a running container."""
    ns = ctx.ns
    name = ns['run.name']
    container_workdir, container_overlays = query_container_info(
        name,
        global_flags=global_flags,
        podman_path=ctx.podman_path,
    )
    if 'user' not in container_overlays.split(','):
        print(
            f'Error: container {name!r} was not created with podrun user overlay.\n'
            f'Cannot auto-attach: exec-entrypoint.sh is not present in the container.\n'
            f'Use --auto-replace instead to replace the container, or remove it with:\n'
            f'  podman rm {name}',
            file=sys.stderr,
        )
        sys.exit(1)
    cmd = (
        [ctx.podman_path]
        + global_flags
        + build_podman_exec_args(
            ns,
            name,
            container_workdir=container_workdir,
            trailing_args=ctx.trailing_args,
            explicit_command=ctx.explicit_command,
        )
    )
    if ns.get('root.print_cmd'):
        print(shlex.join(cmd))
        sys.exit(0)
    _exec_or_subprocess(cmd, os.environ.copy())


def _filter_global_args(global_args: List[str], flags: PodmanFlags) -> List[str]:
    """Single gate for binary flag compatibility in ``podman_global_args``.

    Various callers (``_apply_store``, ``_fuse_overlayfs_fixup``, config
    scripts) inject global flags like ``--root``, ``--runroot``,
    ``--storage-driver``, ``--storage-opt`` in space form (``['--root',
    '/path']``).  Those callers don't need to know which binary is in use —
    this function strips any flag the resolved binary doesn't recognize
    (e.g. storage flags on ``podman-remote``).

    User-typed flags are already safe — argparse only registers flags present
    in the scraped cache, so unsupported flags are never parsed.
    """
    known = flags.global_value_flags | flags.global_boolean_flags
    result: List[str] = []
    i = 0
    while i < len(global_args):
        arg = global_args[i]
        if arg.startswith('-') and arg not in known:
            # Unknown flag — skip it + its value (podrun only injects
            # space-form value flags like ['--root', '/path'])
            i += 2
            continue
        result.append(arg)
        i += 1
    return result


def _filter_conflicting_exports(ns):
    """Remove exports whose container path is already mounted via -v."""
    pt = ns.get('run.passthrough_args') or []
    mount_dests = _volume_mount_destinations(pt)
    filtered = []
    for entry in ns['run.export']:
        cp, _, _ = _parse_export(entry)
        cp = re.sub(r'^~', f'/home/{UNAME}', cp)
        if cp in mount_dests:
            print(
                f'Warning: export {entry!r} skipped — {cp} already mounted via -v',
                file=sys.stderr,
            )
        else:
            filtered.append(entry)
    ns['run.export'] = filtered


def _handle_run(ctx: 'PodrunContext'):  # noqa: C901
    """Handle the ``run`` subcommand: state → entrypoints → overlays → exec.

    This is the main orchestration function.  ``resolve_config()`` and
    ``_apply_store()`` have already run by the time this is called.
    """
    ns = ctx.ns
    global_flags = ns.get('podman_global_args') or []

    # Guard: no image
    if not ctx.trailing_args:
        print(
            'Error: No image specified. Pass image as argument or set "image" in devcontainer.json.',
            file=sys.stderr,
        )
        sys.exit(1)

    # Guard: exports require user overlay
    if (ns.get('run.export') or []) and not ns.get('run.user_overlay'):
        print(
            'Error: --export requires --user-overlay (or an overlay that implies it).',
            file=sys.stderr,
        )
        sys.exit(1)

    # Print overlays
    if ns.get('run.print_overlays'):
        print_overlays()
        sys.exit(0)

    # Set run.image for _env_args (image ref parsing for PODRUN_IMG_* env vars)
    ns['run.image'] = ctx.trailing_args[0]

    # Default prompt banner to image name:tag
    if not ns.get('run.prompt_banner'):
        _, name, tag = _parse_image_ref(ns['run.image'])
        ns['run.prompt_banner'] = f'{name}:{tag}'

    # Set workspace defaults for _host_overlay_args
    if ns.get('run.host_overlay'):
        if not ns.get('dc.workspace_folder'):
            ns['dc.workspace_folder'] = '/app'

    # Container state management
    # For --print-cmd, allow prompts so the printed command reflects the user's choice.
    if (
        ns.get('root.print_cmd')
        and not ns.get('run.auto_attach')
        and not ns.get('run.auto_replace')
    ):
        ns['run.auto_attach'] = None
        ns['run.auto_replace'] = None
    action = handle_container_state(ctx, global_flags=global_flags)
    if action is None:
        sys.exit(0)

    # Config drift check — warn when attach/restart would use stale config
    if action in ('restart', 'attach') and not ns.get('root.print_cmd'):
        action = _check_config_drift(ctx, action)
        if action is None:
            sys.exit(0)

    # Devcontainer initializeCommand — runs on the host during initialization.
    # Fires for every podrun run invocation (create, restart, replace, attach)
    # per the devcontainer spec ("during container creation and on subsequent
    # starts").  Skipped when the devcontainer CLI is driving (it handles its
    # own lifecycle) or when printing the command.
    init_cmd = ns.get('dc.initialize_command')
    if init_cmd and not ctx.dc_from_cli and not ns.get('root.print_cmd'):
        _run_initialize_command(init_cmd)

    replace_rm_cmd = None
    if action == 'replace':
        pm = _shell_quote(ctx.podman_path)
        gf_str = ' '.join(_shell_quote(f) for f in global_flags) + ' ' if global_flags else ''
        replace_rm_cmd = f'{pm} {gf_str}rm -f {_shell_quote(ns["run.name"])}'
        if not ns.get('root.print_cmd'):
            run_os_cmd(replace_rm_cmd)
        action = 'run'

    if action == 'restart':
        name = ns['run.name']
        cmd = [ctx.podman_path] + global_flags + ['start', '-a']
        pt = ns.get('run.passthrough_args') or []
        if ns.get('run.interactive_overlay') or '-i' in pt or '--interactive' in pt:
            cmd.append('-i')
        cmd.append(name)
        if ns.get('root.print_cmd'):
            print(shlex.join(cmd))
            sys.exit(0)
        _exec_or_subprocess(cmd, os.environ.copy())

    if action == 'attach':
        _exec_attach(ctx, global_flags)

    # action == 'run'

    # Filter exports that conflict with existing volume mounts
    if ns.get('run.user_overlay') and (ns.get('run.export') or []):
        _filter_conflicting_exports(ns)

    # Warn about missing subuid/subgid ranges (skip when remote — /etc/subuid
    # inside the container is misleading)
    if ns.get('run.user_overlay') and not _is_remote(ctx.podman_path):
        _warn_missing_subids()

    # Ensure store service is running when using podman-remote with a local store
    if ns.get('run.podman_remote') and ns.get('root.local_store'):
        store_path = pathlib.Path(ns['root.local_store']).resolve()
        graphroot = str(store_path / 'graphroot')
        runroot = _runroot_path(graphroot)
        sock = _ensure_store_service(
            graphroot, runroot, store_dir=str(store_path), podman_path=ctx.podman_path
        )
        ns['run.store_socket'] = sock

    # Overlay mount resolution — handles --fuse-overlayfs storage-opt injection
    # and :O → copy-staging fallback for files / dirs without fuse-overlayfs.
    # Must run before build_overlay_run_command so storage-opt is already in
    # podman_global_args and :O rewrites apply to passthrough args.
    # Skip when remote — storage is on the remote daemon, not local.
    if not _is_remote(ctx.podman_path):
        _resolve_overlay_mounts(ctx)

    # Build the full run command with overlay injection
    cmd, _caps_to_drop = build_overlay_run_command(ctx)

    # Write config sidecar for named containers with user overlay
    if ns.get('run.name') and ns.get('run.user_overlay') and not ns.get('root.print_cmd'):
        _write_config_sidecar(ns)

    if ns.get('root.print_cmd'):
        if replace_rm_cmd:
            print(replace_rm_cmd)
        print(shlex.join(cmd))
        sys.exit(0)

    _exec_or_subprocess(cmd, os.environ.copy())


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv=None):
    raw = argv if argv is not None else sys.argv[1:]

    # Cleanup: early exit before any podman invocation.  Avoids flag
    # scraping touching the store or regenerating the flags cache right
    # before we delete them.
    cleanup_modes = _parse_cleanup_modes(raw)
    if cleanup_modes is not None:
        _handle_cleanup(cleanup_modes)
        sys.exit(0)

    podman_path = _default_podman_path()
    if podman_path is None:
        print('Error: podman not found.', file=sys.stderr)
        sys.exit(1)

    # Stat-based flag loading — zero subprocess calls on warm cache.
    flags = load_podman_flags(podman_path)
    ctx = parse_args(raw, flags=flags)
    ctx.podman_path = podman_path
    ns = ctx.ns

    # Immediate-exit flags
    if ns['root.version']:
        print_version(podman_path)
        sys.exit(0)
    if ns['root.completion']:
        print_completion(ns['root.completion'])

    # Help — pass the raw argv so print_help can check for --help before --
    print_help(ns['subcommand'], raw, podman_path)

    # Config resolution (three-way merge: CLI > config-script > devcontainer.json)
    ctx = resolve_config(ctx)
    ns = ctx.ns

    # NFS remediation — before _apply_store and any storage-touching cmd
    _nfs_remediate(ctx)

    # Store resolution (destroy, resolve, info — all handled inside)
    _apply_store(ctx)

    # Filter global args against the resolved binary's flag set — only needed
    # for podman-remote, which doesn't recognize storage flags like --root,
    # --storage-driver.  Full podman accepts all global args.
    if _is_remote(ctx.podman_path):
        ns['podman_global_args'] = _filter_global_args(ns.get('podman_global_args') or [], flags)

    # Route
    if ns['subcommand'] == 'run':
        _handle_run(ctx)
    elif ns['subcommand'] is not None:
        # Passthrough to podman
        cmd = build_passthrough_command(ctx)
        if ns['root.print_cmd']:
            print(shlex.join(cmd))
            sys.exit(0)
        _exec_or_subprocess(cmd, os.environ.copy())


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit('\nError: KeyboardInterrupt received')
