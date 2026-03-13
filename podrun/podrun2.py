#!/usr/bin/env python3
# Copyright (c) 2026, Kyle Schwab
# All rights reserved.
#
# This source code is licensed under the MIT license found at
# https://github.com/kschwab/podrun/blob/main/LICENSE.md
"""
podrun2
#######

Phase 1.1: argparse-based CLI parsing for podrun.
Phase 1.2: Configuration integration — config-script execution,
           devcontainer.json discovery/parsing, and three-way merge
           (CLI > config-script > devcontainer.json).
Phase 1.3: Local store management — local store resolution, initialization,
           --root/--runroot/--storage-driver injection, and podman
           remote detection.
Phase 2.1: Constants, utilities, and parsing helpers — UID/GID/UNAME identity
           constants, PODRUN_TMP paths, export/image-ref parsing, passthrough
           flag introspection, tilde expansion, SHA-named file writer,
           yes/no prompt.
Phase 2.2: Entrypoint generation — run-entrypoint.sh (user identity, home dir,
           shell, sudo, exports, cap-drop), rc.sh (prompt banner), and
           exec-entrypoint.sh (attach session setup). Functions take ns dict
           directly instead of Config dataclass.
Phase 2.3: Overlay arg builders — user, host, interactive, dot-files, x11,
           podman-remote, env, validation. Cap-drop filtering for user
           --cap-add/--privileged. New --dot-files-overlay (mount-mode).
           print_overlays() implementation.
Phase 2.4: Command assembly + container state — detect_container_state(),
           handle_container_state(), query_container_info(),
           build_podman_exec_args(), build_overlay_run_command().
           Wire entrypoint generation and overlay builders into command
           assembly. Alt-entrypoint extraction for user-overlay.
"""

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
import dataclasses
import hashlib
import json
import os
import pathlib
import platform
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
from typing import List, Tuple

# ---------------------------------------------------------------------------
# Identity and path constants
# ---------------------------------------------------------------------------

UID = os.getuid()
GID = os.getgid()
UNAME = pwd.getpwuid(UID).pw_name
USER_HOME = pwd.getpwuid(UID).pw_dir

PODRUN_TMP = os.path.join(os.environ.get('XDG_RUNTIME_DIR', f'/tmp/podrun-{UID}'), 'podrun')
PODRUN_RC_PATH = '/.podrun/rc.sh'
PODRUN_ENTRYPOINT_PATH = '/.podrun/run-entrypoint.sh'
PODRUN_EXEC_ENTRY_PATH = '/.podrun/exec-entrypoint.sh'
PODRUN_READY_PATH = '/.podrun/READY'
BOOTSTRAP_CAPS = ['CAP_DAC_OVERRIDE', 'CAP_CHOWN', 'CAP_FOWNER', 'CAP_SETPCAP']

# ns-key → PODRUN_OVERLAYS token mapping for _env_args().
_OVERLAY_FIELDS = [
    ('run.user_overlay', 'user'),
    ('run.host_overlay', 'host'),
    ('run.interactive_overlay', 'interactive'),
    ('run.dot_files_overlay', 'dotfiles'),
    ('run.workspace', 'workspace'),
    ('run.adhoc', 'adhoc'),
]

# Mount-mode dotfiles: (relative_path, description).
# Only mounted if they exist on the host.  All are :ro bind mounts.
# Copy-mode dotfiles (.ssh, .gitconfig) deferred to Phase 2.8.
_DOTFILES_MOUNT = [
    '.emacs',
    '.emacs.d',
    '.vimrc',
]

# ---------------------------------------------------------------------------
# CLI flag constants
# ---------------------------------------------------------------------------

# Podrun root flags that overlap with podman global flags and are handled
# by the root parser directly (skip registering as passthrough).
_PODRUN_HANDLED_ROOT_FLAGS = frozenset({'--version', '-v'})

# Podrun run flags that overlap with podman run value flags and are handled
# by the run parser directly (skip registering as passthrough).
_PODRUN_HANDLED_RUN_FLAGS = frozenset({'--name', '--label', '-l'})


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


# In-memory cache keyed by podman_path.
_loaded_flags = {}


def get_podman_version(podman_path):
    """Parse version string from ``podman --version``."""
    result = subprocess.run(
        [podman_path, '--version'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    if result.returncode != 0:
        return None
    # "podman version 5.4.0" → "5.4.0"
    m = re.search(r'(\d+\.\d+\.\d+)', result.stdout)
    return m.group(1) if m else None


def _flags_cache_dir():
    """Return ``$XDG_CACHE_HOME/podrun`` or ``~/.cache/podrun``."""
    base = os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache')
    return os.path.join(base, 'podrun')


def _flags_cache_path(version):
    """Return the cache file path for a given podman version."""
    return os.path.join(_flags_cache_dir(), f'podman-{version}.json')


def _scrape_all_flags(podman_path):
    """Scrape global and run flags from podman --help and return a PodmanFlags."""
    global_result = _scrape_podman_help(podman_path)
    if global_result is None:
        raise RuntimeError(f'Failed to scrape {podman_path} --help')

    global_value, global_bool, subcmds = global_result
    # Filter out 'help' — podman lists it but we don't register it as a subparser.
    subcmds.discard('help')

    run_result = _scrape_podman_help(podman_path, subcmd='run')
    if run_result is None:
        raise RuntimeError(f'Failed to scrape {podman_path} run --help')

    run_value, run_bool, _ = run_result

    return PodmanFlags(
        global_value_flags=frozenset(global_value),
        global_boolean_flags=frozenset(global_bool),
        subcommands=frozenset(subcmds),
        run_value_flags=frozenset(run_value),
        run_boolean_flags=frozenset(run_bool),
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
        )
    except (OSError, KeyError, json.JSONDecodeError):
        return None


def _write_flags_cache(path, flags):
    """Write a PodmanFlags to a JSON cache file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {
        'global_value_flags': sorted(flags.global_value_flags),
        'global_boolean_flags': sorted(flags.global_boolean_flags),
        'subcommands': sorted(flags.subcommands),
        'run_value_flags': sorted(flags.run_value_flags),
        'run_boolean_flags': sorted(flags.run_boolean_flags),
    }
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


def load_podman_flags(podman_path='podman'):
    """Load podman flags via in-memory cache, disk cache, or live scrape.

    Resolution chain:
    1. In-memory cache hit → return immediately
    2. Disk cache for current podman version → read, store in memory, return
    3. Scrape local podman (error if remote-only) → write cache, return
    4. Podman not found → sys.exit(1)
    """
    if podman_path in _loaded_flags:
        return _loaded_flags[podman_path]

    version = get_podman_version(podman_path)
    if version is None:
        print(f'Error: Could not determine podman version from {podman_path}', file=sys.stderr)
        sys.exit(1)

    # Try disk cache
    cache_path = _flags_cache_path(version)
    flags = _read_flags_cache(cache_path)
    if flags is not None:
        _loaded_flags[podman_path] = flags
        return flags

    # Must scrape — refuse if remote-only
    if is_podman_remote(podman_path):
        print(
            f'Error: {podman_path} is a remote client (no local engine).\n'
            'Cannot scrape flags from a remote client and no cache file found.\n'
            f'Expected cache at: {cache_path}',
            file=sys.stderr,
        )
        sys.exit(1)

    flags = _scrape_all_flags(podman_path)
    _write_flags_cache(cache_path, flags)
    _loaded_flags[podman_path] = flags
    return flags


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def run_os_cmd(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=True,
        universal_newlines=True,
    )


def is_podman_remote(podman_path: str) -> bool:
    """Return True if *podman_path* is a remote-only client (no local engine)."""
    result = run_os_cmd(f'{shlex.quote(podman_path)} info --format {{{{.Host.ServiceIsRemote}}}}')
    return result.returncode == 0 and result.stdout.strip() == 'true'


def run_config_scripts(script_paths: List[str]) -> List[str]:
    """Execute scripts left-to-right, return concatenated shlex.split tokens.

    Fatal (sys.exit(1)) on non-zero exit.
    """
    tokens: List[str] = []
    for path in script_paths:
        out = run_os_cmd(shlex.quote(path))
        if out.returncode != 0:
            print(
                f'Error: --config-script {path} failed '
                f'(exit {out.returncode}):\n{out.stderr}',
                file=sys.stderr,
            )
            sys.exit(1)
        tokens.extend(shlex.split(out.stdout))
    return tokens


def parse_config_tokens(tokens: List[str], flags=None) -> Tuple[dict, List[str]]:
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
    _FORBIDDEN = {'--config', '--config-script', '--no-devconfig'}
    found = _FORBIDDEN.intersection(tokens)
    if found:
        print(
            f'Error: config-script output must not contain {", ".join(sorted(found))}',
            file=sys.stderr,
        )
        sys.exit(1)

    root = build_root_parser(flags)

    # Suppress subcommand validation — config tokens have no subcommand.
    # Remove the subcommand subparsers action so positionals don't trigger
    # "invalid choice" errors.
    saved_actions = root._subparsers._group_actions[:]
    root._subparsers._group_actions.clear()
    # Also remove the subparsers action from _actions to prevent positional matching
    saved_sub_actions = [a for a in root._actions if isinstance(a, argparse._SubParsersAction)]
    for a in saved_sub_actions:
        root._actions.remove(a)

    root_ns, unknowns = root.parse_known_args(tokens)
    root_dict = vars(root_ns)

    # Restore actions
    root._subparsers._group_actions.extend(saved_actions)
    root._actions.extend(saved_sub_actions)

    # Second pass: run parser on unknowns
    run_parser = root._run_subparser
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

    # Podman passthrough = unknowns from the run parser + any run.passthrough_args
    run_passthrough = run_dict.get('run.passthrough_args') or []
    podman_passthrough = podman_passthrough + run_passthrough

    return config_ns, podman_passthrough


# ---------------------------------------------------------------------------
# Phase 2.1 — Parsing helpers and utilities
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


def _parse_export(entry: str):
    """Parse an export entry into ``(container_path, host_path, copy_only)``.

    Accepted forms::

        container_path:host_path        — strict (rm + symlink)
        container_path:host_path:0      — copy-only (populate host dir, skip rm/symlink)
    """
    parts = entry.split(':')
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
    """Write content to a SHA-named file in PODRUN_TMP.  Idempotent."""
    content_hash = hashlib.sha256(content.encode()).hexdigest()[:12]
    filename = f'{prefix}{content_hash}{suffix}'
    path = os.path.join(PODRUN_TMP, filename)
    if not os.path.exists(path):
        pathlib.Path(PODRUN_TMP).mkdir(parents=True, exist_ok=True)
        with open(path, 'w') as f:
            f.write(content)
        os.chmod(path, 0o755)
    return path


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
        if a.startswith('-') and not a.startswith('--'):
            if char in a[1:]:
                return True
    return False


def _extract_label_value(pt, label_key):
    """Extract a label value from passthrough args.

    Searches for ``-l key=value``, ``--label=key=value``, etc.
    Returns the value, or ``None`` if not found.
    """
    prefix = f'{label_key}='
    i = 0
    while i < len(pt):
        arg = pt[i]
        if arg.startswith(('--label=', '-l=')):
            val = arg.split('=', 1)[1]
            if val.startswith(prefix):
                return val[len(prefix):]
        elif arg in ('-l', '--label') and i + 1 < len(pt):
            val = pt[i + 1]
            if val.startswith(prefix):
                return val[len(prefix):]
            i += 2
            continue
        i += 1
    return None


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


def _volume_mount_destinations(*arg_lists) -> set:
    """Extract container destination paths from -v/--volume args across all arg lists."""
    dests = set()
    for args in arg_lists:
        for arg in args:
            m = re.match(r'^(-v|--volume)=(.*)', arg)
            if not m:
                continue
            parts = m.group(2).split(':')
            if len(parts) >= 2:
                dest = re.sub(r'^~', f'/home/{UNAME}', parts[1])
                dests.add(dest)
    return dests


# ---------------------------------------------------------------------------
# Tilde expansion
# ---------------------------------------------------------------------------


def _expand_volume_tilde(args: list) -> list:
    """Expand ``~`` in ``-v``/``--volume`` arguments.

    Source (host) ``~`` expands to USER_HOME.
    Destination (container) ``~`` expands to ``/home/{UNAME}``.

    Handles both equals form (``-v=~/src:/dst``) and space-separated
    form (``-v``, ``~/src:/dst``) as produced by ``_PassthroughAction``.
    """
    result = []
    i = 0
    while i < len(args):
        arg = args[i]
        # Equals form: -v=~/src:/dst or --volume=~/src:/dst
        m = re.match(r'^(-v|--volume)=(.*)', arg)
        if m:
            flag = m.group(1)
            parts = m.group(2).split(':')
            if len(parts) >= 2:
                parts[0] = re.sub(r'^~', USER_HOME, parts[0])
                parts[1] = re.sub(r'^~', f'/home/{UNAME}', parts[1])
            elif len(parts) == 1:
                parts[0] = re.sub(r'^~', USER_HOME, parts[0])
            result.append(f'{flag}={":".join(parts)}')
            i += 1
            continue
        # Space form: -v ~/src:/dst or --volume ~/src:/dst
        if arg in ('-v', '--volume') and i + 1 < len(args):
            val = args[i + 1]
            parts = val.split(':')
            if len(parts) >= 2:
                parts[0] = re.sub(r'^~', USER_HOME, parts[0])
                parts[1] = re.sub(r'^~', f'/home/{UNAME}', parts[1])
            elif len(parts) == 1:
                parts[0] = re.sub(r'^~', USER_HOME, parts[0])
            result.append(arg)
            result.append(':'.join(parts))
            i += 2
            continue
        result.append(arg)
        i += 1
    return result


def _expand_export_tilde(exports: list) -> list:
    """Expand ``~`` in export entries (``container_path:host_path[:0]``).

    Host ``~`` expands to USER_HOME, container ``~`` expands to ``/home/{UNAME}``.
    """
    result = []
    for entry in exports:
        parts = entry.split(':')
        if len(parts) >= 2:
            parts[0] = re.sub(r'^~', f'/home/{UNAME}', parts[0])
            parts[1] = re.sub(r'^~', USER_HOME, parts[1])
        elif len(parts) == 1:
            parts[0] = re.sub(r'^~', f'/home/{UNAME}', parts[0])
        result.append(':'.join(parts))
    return result


# ---------------------------------------------------------------------------
# Phase 2.2 — Entrypoint generation
# ---------------------------------------------------------------------------


def generate_run_entrypoint(ns: dict, caps_to_drop: list = None) -> str:
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

    script = textwrap.dedent(f'''\
        #!/bin/sh{login_flag}
        # Generated by podrun {__version__}. Do not modify by hand.
        set -e

{shell_detect}
        PODRUN_SHELL="$SHELL"; export PODRUN_SHELL

        # Patch SHELL field in /etc/passwd (--passwd-entry creates the entry
        # with /bin/sh; update to the resolved shell path).
        # Also ensure group entry exists (requires CAP_DAC_OVERRIDE).
        if command -v sed > /dev/null 2>&1; then
          sed -i "s|^\\({UNAME}:.*:\\)/bin/sh\\$|\\1$SHELL|" /etc/passwd 2>/dev/null || true
        fi
        if ! awk -v gid={GID} -F: '{{ if($3==gid){{found=1}} }} END{{exit !found}}' /etc/group 2>/dev/null; then
          echo "{UNAME}:x:{GID}:" >> /etc/group 2>/dev/null || true
        fi

        # Create home directory and populate from /etc/skel
        # (requires CAP_DAC_OVERRIDE for /etc/skel access, CAP_CHOWN for ownership)
        # Uses -xdev to skip bind-mounted files/dirs (different filesystem).
        mkdir -p /home/{UNAME}
        if [ -d /etc/skel ]; then
          cp -a /etc/skel/. /home/{UNAME}/ 2>/dev/null || true
        fi
        find /home/{UNAME} -xdev -exec chown {UID}:{GID} {{}} + 2>/dev/null || true

        # Opportunistic sudo setup (requires CAP_DAC_OVERRIDE to write sudoers)
        if command -v sudo > /dev/null 2>&1; then
          echo "{UNAME} ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers 2>/dev/null || true
        fi

        # Convenience symlink to workspace
        ln -s "$PWD" /home/{UNAME}/workdir > /dev/null 2>&1 || true

        # Wire rc.sh into bashrc
        _bashrc="/home/{UNAME}/.bashrc"
        if [ ! -f "$_bashrc" ] || ! grep -q '{PODRUN_RC_PATH}' "$_bashrc" 2>/dev/null; then
          echo '. {PODRUN_RC_PATH}' >> "$_bashrc"
        fi

        # Force HOME to the directory we just created — the image may have
        # HOME baked in (e.g. ENV HOME=/root) which prevents podman from
        # deriving it from --passwd-entry.
        HOME=/home/{UNAME}
        export HOME
        ENV={PODRUN_RC_PATH}
        export ENV

{export_blocks}        # Signal that setup is complete so exec-entrypoint.sh can proceed.
        touch {PODRUN_READY_PATH}

        # If an alternate entrypoint was requested (e.g. by the devcontainer
        # CLI via --entrypoint), prepend it to the args so it is exec'd after
        # our setup completes.  The podrun entrypoint always runs first to
        # ensure user identity and home directory are ready.
        if [ -n "$PODRUN_ALT_ENTRYPOINT" ]; then
          set -- "$PODRUN_ALT_ENTRYPOINT" "$@"
        fi

        # Drop bootstrap capabilities before exec.
        # Probe short names first (BusyBox), fall back to cap_ prefix (util-linux).
        # Drop from both inheritable and ambient sets so effective caps
        # are cleared after exec (ambient caps drive effective in userns).
        if command -v setpriv > /dev/null 2>&1; then
          _drop="{','.join('-' + (c[4:] if c.startswith('CAP_') else c).lower() for c in caps_to_drop)}"
          if ! setpriv --inh-caps="$_drop" --ambient-caps="$_drop" true 2>/dev/null; then
            _drop="{','.join('-cap_' + (c[4:] if c.startswith('CAP_') else c).lower() for c in caps_to_drop)}"
          fi
          if [ $# -eq 0 ]; then
            exec setpriv --inh-caps="$_drop" --ambient-caps="$_drop" $SHELL
          else
            exec setpriv --inh-caps="$_drop" --ambient-caps="$_drop" "$@"
          fi
        elif command -v capsh > /dev/null 2>&1; then
          # capsh uses cap_xxx names; --delamb removes from ambient,
          # --drop removes from bounding.
          _capsh_args=""
          # shellcheck disable=SC2043
          for _cap in {' '.join('cap_' + (c[4:] if c.startswith('CAP_') else c).lower() for c in caps_to_drop)}; do
            _capsh_args="$_capsh_args --delamb=$_cap --drop=$_cap"
          done
          # shellcheck disable=SC2086
          if [ $# -eq 0 ]; then
            exec capsh $_capsh_args -- -c "exec $SHELL"
          else
            _quoted=""
            for _arg in "$@"; do
              _quoted="$_quoted '$_arg'"
            done
            exec capsh $_capsh_args -- -c "exec $_quoted"
          fi
        else
          if [ $# -eq 0 ]; then
            exec $SHELL
          else
            exec "$@"
          fi
        fi
    ''')
    return _write_sha_file(script, 'entrypoint_', '.sh')


def generate_rc_sh(ns: dict) -> str:
    """Generate the rc.sh prompt/banner script and return its path (SHA-named, idempotent).

    Reads from the *ns* dict: ``run.prompt_banner``.
    """
    prompt_banner = ns.get('run.prompt_banner') or 'podrun'
    cpu_name = run_os_cmd(
        "grep -m 1 'model name[[:space:]]*:' /proc/cpuinfo"
        " | cut -d ' ' -f 3- | sed 's/(R)/\u00ae/g; s/(TM)/\u2122/g;'"
    ).stdout
    cpu_vcount = run_os_cmd("grep -o 'processor[[:space:]]*:' /proc/cpuinfo | wc -l").stdout
    cpu = f'{cpu_name.strip()} ({cpu_vcount.strip()} vCPU)'
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
        _prompt_banner="{prompt_banner}"
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


def generate_exec_entrypoint() -> str:
    """Generate exec-entrypoint.sh and return its path (SHA-named, idempotent).

    The exec-entrypoint is configuration-independent — it reads ``PODRUN_*``
    env vars at runtime (set by ``podman run -e``).  No *ns* dict needed.
    """
    script = textwrap.dedent(f"""\
        #!/bin/sh
        # Generated by podrun {__version__}. Do not modify by hand.

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
        _home="$(awk -v uid=$(id -u) -F: '$3==uid{{print $6}}' /etc/passwd 2>/dev/null)"
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
          _shell="$(awk -v uid=$(id -u) -F: '$3==uid{{print $NF}}' /etc/passwd 2>/dev/null)"
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
# Phase 2.3 — Overlay arg builders
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
    if not _passthrough_has_flag(pt, '--passwd-entry'):
        args.append(f'--passwd-entry={UNAME}:*:{UID}:{GID}:{UNAME}:/home/{UNAME}:/bin/sh')
    caps_to_drop = compute_caps_to_drop(pt)
    for cap in BOOTSTRAP_CAPS:
        args.append(f'--cap-add={cap}')
    args.append(f'--entrypoint={PODRUN_ENTRYPOINT_PATH}')
    args.append(f'-v={entrypoint_path}:{PODRUN_ENTRYPOINT_PATH}:ro')
    args.append(f'-v={rc_path}:{PODRUN_RC_PATH}:ro')
    args.append(f'-v={exec_entry_path}:{PODRUN_EXEC_ENTRY_PATH}:ro')
    args.append(f'--env=ENV={PODRUN_RC_PATH}')
    for entry in ns.get('run.export') or []:
        container_path, host_path, _ = _parse_export(entry)
        abs_host = os.path.abspath(host_path)
        os.makedirs(abs_host, exist_ok=True)
        staging_hash = hashlib.sha256(container_path.encode()).hexdigest()[:12]
        args.append(f'-v={abs_host}:/.podrun/exports/{staging_hash}')
    return args, caps_to_drop


def _interactive_overlay_args(ns, pt):
    """Build args for --interactive-overlay: interactive session flags."""
    args = []
    if not (_passthrough_has_short_flag(pt, 'i') or _passthrough_has_short_flag(pt, 't')):
        args.append('-it')
    args.append('--detach-keys=ctrl-q,ctrl-q')
    return args


def _host_overlay_args(ns, pt):
    """Build args for --host-overlay: overlay host system context onto container."""
    workspace_folder = ns.get('run.workspace_folder') or '/app'
    workspace_mount_src = ns.get('run.workspace_mount_src') or str(pathlib.Path.cwd())
    args = []
    if not _passthrough_has_flag(pt, '--hostname'):
        args.append(f'--hostname={platform.node()}')
    if not _passthrough_has_flag(pt, '--network'):
        args.append('--network=host')
    if not _passthrough_has_exact(pt, '--security-opt=seccomp=unconfined'):
        args.append('--security-opt=seccomp=unconfined')
    if not _passthrough_has_exact(pt, '--init'):
        args.append('--init')
    args.append(f'-v={workspace_mount_src}:{workspace_folder}')
    if not _passthrough_has_flag(pt, '-w') and not _passthrough_has_flag(pt, '--workdir'):
        args.append(f'-w={workspace_folder}')
    if not _passthrough_has_exact(pt, '--env=TERM=xterm-256color'):
        args.append('--env=TERM=xterm-256color')
    if os.path.exists('/etc/localtime'):
        args.append('-v=/etc/localtime:/etc/localtime:ro')
    return args


def _dot_files_overlay_args(ns, pt):
    """Build args for --dot-files-overlay: mount-mode dotfiles from host HOME."""
    args = []
    for name in _DOTFILES_MOUNT:
        host_path = os.path.join(USER_HOME, name)
        if os.path.exists(host_path):
            container_path = f'/home/{UNAME}/{name}'
            args.append(f'-v={host_path}:{container_path}:ro')
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
    """Build args for podman-remote (rootless Podman socket passthrough)."""
    args = []
    store_socket = ns.get('run.store_socket')
    if store_socket and pathlib.Path(store_socket).exists():
        args.append(f'-v={store_socket}:/run/podman/podman.sock')
        args.append('--env=CONTAINER_HOST=unix:///run/podman/podman.sock')
    else:
        podman_socket = f'/run/user/{UID}/podman/podman.sock'
        if pathlib.Path(podman_socket).exists():
            args.append(f'-v={podman_socket}:/run/podman/podman.sock')
            args.append('--env=CONTAINER_HOST=unix:///run/podman/podman.sock')
        else:
            print('Warning: podman remote was requested but podman.socket not found.', file=sys.stderr)
            print('systemctl --user enable --now podman.socket', file=sys.stderr)
    return args


def _env_args(ns):
    """Build args for container environment variables and PODRUN_* env vars."""
    args = []
    for key, val in (ns.get('run.remote_env') or {}).items():
        args.append(f'--env={key}={val}')

    overlays = [name for ns_key, name in _OVERLAY_FIELDS if ns.get(ns_key)]
    overlay_str = ','.join(overlays) if overlays else 'none'
    args.append(f'--env=PODRUN_OVERLAYS={overlay_str}')

    if ns.get('run.host_overlay'):
        workspace_folder = ns.get('run.workspace_folder') or '/app'
        args.append(f'--env=PODRUN_WORKDIR={workspace_folder}')
    if ns.get('run.shell'):
        args.append(f'--env=PODRUN_SHELL={ns["run.shell"]}')
    if ns.get('run.login') is not None:
        args.append(f'--env=PODRUN_LOGIN={"1" if ns["run.login"] else "0"}')

    image = ns.get('run.image')
    if image:
        repo, name, tag = _parse_image_ref(image)
        args.append(f'--env=PODRUN_IMG={image}')
        args.append(f'--env=PODRUN_IMG_NAME={name}')
        args.append(f'--env=PODRUN_IMG_REPO={repo}')
        args.append(f'--env=PODRUN_IMG_TAG={tag}')
    return args


def _validate_overlay_args(ns):
    """Error on args that conflict with enabled overlays."""
    if not ns.get('run.user_overlay'):
        return
    all_args = ns.get('run.passthrough_args') or []

    for arg in all_args:
        if arg.startswith('--userns'):
            continue
        if (
            arg == '--user'
            or arg.startswith('--user=')
            or arg == '-u'
            or (arg.startswith('-u') and not arg.startswith('--') and len(arg) > 2)
        ):
            print(
                f'Error: {arg} conflicts with --user-overlay.\n'
                'user-overlay maps host identity via --userns=keep-id and --passwd-entry.\n'
                'Remove --user-overlay or remove the --user flag.',
                file=sys.stderr,
            )
            sys.exit(1)

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
    print('    --passwd-entry=<user>:*:<uid>:<gid>:<user>:/home/<user>:/bin/sh')
    print(f'    --cap-add={",".join(BOOTSTRAP_CAPS)}  (dropped after entrypoint)')
    print(f'    --entrypoint={PODRUN_ENTRYPOINT_PATH}')
    print(f'    -v=<run-entrypoint>:{PODRUN_ENTRYPOINT_PATH}:ro')
    print(f'    -v=<rc.sh>:{PODRUN_RC_PATH}:ro')
    print(f'    -v=<exec-entrypoint>:{PODRUN_EXEC_ENTRY_PATH}:ro')
    print()
    print('  host (implies user):')
    print('    --user-overlay')
    print(f'    --hostname={platform.node()}')
    print('    --network=host')
    print('    --security-opt=seccomp=unconfined')
    print('    --init')
    print('    -v=<cwd>:<workspaceFolder>')
    print('    -w=<workspaceFolder>')
    print('    --env=TERM=xterm-256color')
    print()
    print('  interactive:')
    print('    -it')
    print('    --detach-keys=ctrl-q,ctrl-q')
    print()
    print('  dotfiles (implies user):')
    print('    --user-overlay')
    for name in _DOTFILES_MOUNT:
        print(f'    -v=~/{name}:/home/<user>/{name}:ro  (if exists)')
    print()
    print('  workspace (implies host + interactive):')
    print('    --host-overlay')
    print('    --interactive-overlay')
    print()
    print('  adhoc (implies workspace):')
    print('    --workspace')
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
    return devcontainer.get('customizations', {}).get('podrun', {})


def devcontainer_run_args(devcontainer: dict) -> list:
    """Convert devcontainer.json top-level fields to podman run args."""
    args: list = []

    for mount in devcontainer.get('mounts', []):
        if isinstance(mount, dict):
            parts = ','.join(f'{k}={v}' for k, v in mount.items())
            args.append(f'--mount={parts}')
        else:
            args.append(f'--mount={mount}')

    for cap in devcontainer.get('capAdd', []):
        args.append(f'--cap-add={cap}')

    for opt in devcontainer.get('securityOpt', []):
        args.append(f'--security-opt={opt}')

    if devcontainer.get('privileged', False):
        args.append('--privileged')

    if devcontainer.get('init', False):
        args.append('--init')

    args.extend(devcontainer.get('runArgs', []))

    return args


# ---------------------------------------------------------------------------
# Store management
# ---------------------------------------------------------------------------

_PODRUN_STORES_DIR = '/tmp/podrun-stores'


def _runroot_path(graphroot: str) -> str:
    """Return a deterministic runroot path under ``/tmp`` for *graphroot*.

    Uses a short SHA-256 prefix to stay well within
    the 108-byte ``sun_path`` limit and is unique per graphroot.
    """
    h = hashlib.sha256(graphroot.encode()).hexdigest()[:12]
    return f'{_PODRUN_STORES_DIR}/{h}'


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
    graphroot.mkdir(parents=True, exist_ok=True)

    # Runroot under /tmp (deterministic, short path)
    runroot_target = _runroot_path(str(graphroot))
    pathlib.Path(runroot_target).mkdir(parents=True, exist_ok=True)

    # Symlink store_dir/runroot → /tmp/podrun-stores/<hash>/
    runroot_link = store_path / 'runroot'
    if runroot_link.is_symlink() or runroot_link.exists():
        runroot_link.unlink()
    runroot_link.symlink_to(runroot_target)


def _store_print_info(store_dir: str) -> None:
    """Print summary information about a podrun store."""
    store_path = pathlib.Path(store_dir).resolve()
    rel_store = os.path.relpath(store_path)
    graphroot = store_path / 'graphroot'

    if not graphroot.is_dir():
        print(f'Local store: {rel_store} (not initialized)')
        return

    runroot_link = store_path / 'runroot'
    runroot_target = os.readlink(str(runroot_link)) if runroot_link.is_symlink() else '?'
    runroot_exists = os.path.isdir(runroot_target) if runroot_target != '?' else False

    print(f'Local store: {rel_store}')
    print(f'  graphroot:  {rel_store}/graphroot')
    runroot_status = '' if runroot_exists else '  (missing — will be created on use)'
    print(f'  runroot:    {rel_store}/runroot → {runroot_target}{runroot_status}')


def _stop_store_service(graphroot: str) -> None:
    """Stop the podman system service for a store, if running.

    TODO: phase 2 — full service management.
    """


def _store_destroy(store_dir: str, podman_path: str) -> None:
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


def _resolve_store(ns: dict, podman_path: str = 'podman') -> Tuple[List[str], dict]:
    """Resolve store directory into podman global flags.

    Returns ``(flags_list, env_dict)`` where *flags_list* contains
    ``['--root', ..., '--runroot', ...]`` (and ``--storage-driver`` when
    not already supplied via podman global args) or empty if no store is
    active.

    If ``--storage-driver`` is already present in ``podman_global_args``
    (i.e. the user passed it explicitly), that value is respected and
    the local store does not inject a redundant ``--storage-driver``.
    """
    # --local-store-ignore → skip store entirely
    if ns.get('root.local_store_ignore'):
        return [], {}

    store_dir = ns.get('root.local_store')

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
        _store_destroy(store_dir, podman_path)

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
    pathlib.Path(runroot).mkdir(parents=True, exist_ok=True)

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


def _apply_store(ns: dict, podman_path: str = 'podman') -> None:
    """Resolve store, prepend flags, and handle store-only exits.

    With podman remote the store is determined by the service socket
    established at container launch — local store flags do not apply.
    """
    remote = is_podman_remote(podman_path)

    if ns.get('root.local_store_destroy') and remote:
        print('Error: --local-store-destroy not supported with podman remote', file=sys.stderr)
        sys.exit(1)

    if not remote:
        flags, _env = _resolve_store(ns, podman_path)
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
    'storageDriver': 'root.storage_driver',
}

_RUN_CONFIG_MAP = {
    'name': 'run.name',
    'userOverlay': 'run.user_overlay',
    'hostOverlay': 'run.host_overlay',
    'interactiveOverlay': 'run.interactive_overlay',
    'workspace': 'run.workspace',
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


def resolve_config(result: 'ParseResult', flags=None) -> 'ParseResult':  # noqa: C901 — three-way merge across CLI, config-script, and devcontainer.json
    """Three-way merge: CLI > config-script > devcontainer.json.

    Updates result.ns in place and attaches context.
    """

    def _first(*values):
        for v in values:
            if v is not None:
                return v
        return None

    ns = result.ns

    # 1. Load devcontainer.json — honor root.config / root.no_devconfig / run.label
    dc = {}
    dc_path = None
    podrun_cfg = {}

    if not ns.get('root.no_devconfig'):
        # Check for label-based dc selection
        label_config_path = None
        for lbl in ns.get('run.label') or []:
            if lbl.startswith('devcontainer.config_file='):
                label_config_path = lbl.split('=', 1)[1]

        if ns.get('root.config'):
            dc_path = ns['root.config']
        elif label_config_path:
            dc_path = label_config_path
        else:
            dc_path = find_devcontainer_json()

        if dc_path is not None:
            dc = parse_devcontainer_json(dc_path)

        # 3. Extract customizations.podrun
        podrun_cfg = extract_podrun_config(dc)

    # 4. Determine scripts — devcontainer configScript first, then CLI --config-script
    script_paths = []
    dc_script = podrun_cfg.get('configScript')
    if dc_script:
        script_paths.extend([dc_script] if isinstance(dc_script, str) else dc_script)
    cli_scripts = ns.get('root.config_script')
    if cli_scripts:
        script_paths.extend(cli_scripts)

    # 5. Execute scripts → run_config_scripts() → parse_config_tokens()
    script_ns = {}
    script_passthrough = []
    if script_paths:
        script_tokens = run_config_scripts(script_paths)
        script_ns, script_passthrough = parse_config_tokens(script_tokens, flags)

    # 6. Convert devcontainer config → _devcontainer_to_ns() + devcontainer_run_args()
    dc_ns = _devcontainer_to_ns(podrun_cfg)
    dc_run_args = devcontainer_run_args(dc)

    # 7. Merge scalars — _first(cli_ns, script_ns, dc_ns) per key
    all_keys = set()
    for k in ns:
        if k.startswith('root.') or k.startswith('run.'):
            all_keys.add(k)
    all_keys.update(script_ns.keys())
    all_keys.update(dc_ns.keys())

    for key in all_keys:
        cli_val = ns.get(key)
        script_val = script_ns.get(key)
        dc_val = dc_ns.get(key)
        merged = _first(cli_val, script_val, dc_val)
        if merged is not None:
            ns[key] = merged

    # 8. Prepend podman args — DC run args first (lowest priority), then script,
    #    then CLI passthrough (already in the list, highest priority).
    existing_passthrough = ns.get('run.passthrough_args') or []
    ns['run.passthrough_args'] = dc_run_args + script_passthrough + existing_passthrough

    # 9. Handle run specifics
    if ns.get('subcommand') == 'run':
        # Overlay implication chain: adhoc→workspace→host+interactive→user
        #                           dot_files→user
        if ns.get('run.adhoc'):
            ns['run.workspace'] = True
        if ns.get('run.workspace'):
            ns['run.host_overlay'] = True
            ns['run.interactive_overlay'] = True
        if ns.get('run.host_overlay'):
            ns['run.user_overlay'] = True
        if ns.get('run.dot_files_overlay'):
            ns['run.user_overlay'] = True

        # Image/command resolution: CLI trailing > devcontainer image
        dc_image = dc.get('image')
        if not result.trailing_args and dc_image:
            result.trailing_args = [dc_image]

        # Exports append: dc + script + cli
        dc_exports = podrun_cfg.get('exports', [])
        script_exports = script_ns.get('run.export') or []
        cli_exports = ns.get('run.export') or []
        combined_exports = dc_exports + script_exports + cli_exports
        if combined_exports:
            ns['run.export'] = combined_exports

    # 10. Attach context for Phase 2
    result._devcontainer = dc
    result._podrun_cfg = podrun_cfg

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
    )
    opts = parser.add_argument_group('Options')

    # -- Podrun global flags (dest='root_*') ----------------------------------
    opts.add_argument(
        '--print-cmd',
        '--dry-run',
        dest='root.print_cmd',
        action='store_true',
        default=False,
        help='Print the podman command instead of executing it',
    )
    opts.add_argument(
        '--config',
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
        help='Run script and inline its stdout as args (may be repeated)',
    )
    opts.add_argument(
        '--no-devconfig',
        dest='root.no_devconfig',
        action='store_true',
        default=False,
        help='Skip devcontainer.json discovery',
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
        default=False,
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
        default=False,
        help='Suppress auto-discovery of project-local store',
    )
    opts.add_argument(
        '--local-store-auto-init',
        dest='root.local_store_auto_init',
        action='store_true',
        default=False,
        help='Auto-create store if missing (requires --local-store)',
    )
    opts.add_argument(
        '--local-store-info',
        dest='root.local_store_info',
        action='store_true',
        default=False,
        help='Print store information and exit',
    )
    opts.add_argument(
        '--local-store-destroy',
        dest='root.local_store_destroy',
        action='store_true',
        default=False,
        help='Remove project-local store before proceeding',
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

    # -- Subparsers for routing -----------------------------------------------
    subs = parser.add_subparsers(dest='subcommand', title='Available Commands')
    subs.required = False  # Allow no subcommand (for --version, --help, etc.)

    # Real subparsers for podrun commands (full flag parsing)
    run_parser = _build_run_subparser(subs, flags.run_value_flags, flags.run_boolean_flags)

    # Empty subparsers for podman passthrough commands
    for subcmd in sorted(flags.subcommands - {'run'}):
        subs.add_parser(subcmd, add_help=False)

    # Stash for help/completion access
    parser._run_subparser = run_parser

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
        description='Additional run options for host identity overlays.',
    )
    opts = parser.add_argument_group('Options')

    # -- Podrun run flags (dest='run_*') --------------------------------------
    opts.add_argument('--name', dest='run.name', metavar='NAME', help=argparse.SUPPRESS)
    opts.add_argument(
        '--label', '-l', dest='run.label', action='append',
        default=None, metavar='KEY=VALUE', help=argparse.SUPPRESS,
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
        '--workspace',
        dest='run.workspace',
        action='store_true',
        default=None,
        help='Workspace overlay (implies --host-overlay + --interactive-overlay)',
    )
    opts.add_argument(
        '--adhoc',
        dest='run.adhoc',
        action='store_true',
        default=None,
        help='Ad-hoc overlay (implies --workspace + --rm)',
    )
    opts.add_argument(
        '--dot-files-overlay', '--dotfiles',
        dest='run.dot_files_overlay',
        action='store_true',
        default=None,
        help='Mount host dotfiles into container (implies --user-overlay)',
    )
    opts.add_argument(
        '--print-overlays',
        dest='run.print_overlays',
        action='store_true',
        default=False,
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

    # -- IMAGE [COMMAND [ARG...]] boundary ------------------------------------
    # REMAINDER stops flag parsing at the first positional so that command
    # args like ``bash -c echo`` are not consumed as podman flags.
    parser.add_argument('run.trailing', nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    return parser


# ---------------------------------------------------------------------------
# ParseResult
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class ParseResult:
    """Structured result from :func:`parse_args`.

    Access parsed values through the dict using prefix conventions::

        result.ns['subcommand']              # 'run', 'store', 'ps', etc. or None
        result.ns['root.print_cmd']          # global podrun config flags
        result.ns.get('podman_global_args') or []  # ['--root', '/x', '--remote', ...]
    """

    ns: dict
    trailing_args: List[str]  # For run: image + command
    explicit_command: List[str]  # Args after '--'
    raw_argv: List[str]  # Original argv
    subcmd_passthrough_args: List[str]  # For passthrough subcommands


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------


def parse_args(argv: List[str], flags=None) -> ParseResult:
    """Parse podrun CLI arguments and return a structured :class:`ParseResult`.

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

    # Single-pass parse: root parser handles global flags + subcommand routing;
    # real subparsers (run/store) handle subcommand-specific flags.
    root = build_root_parser(flags)
    ns_raw, unknowns = root.parse_known_args(flag_section)
    ns = vars(ns_raw)

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

    return ParseResult(
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
    name: str, global_flags=None, podman_path: str = 'podman',
):
    """Returns ``"running"``, ``"stopped"``, or ``None``."""
    if not name:
        return None
    gf = ' '.join(shlex.quote(f) for f in global_flags) + ' ' if global_flags else ''
    fmt = shlex.quote('{{.State.Status}}')
    result = run_os_cmd(
        f'{shlex.quote(podman_path)} {gf}inspect --format={fmt} {shlex.quote(name)}'
    )
    if result.returncode != 0:
        return None
    status = result.stdout.strip()
    if status == 'running':
        return 'running'
    if status in ('created', 'exited', 'stopped', 'dead', 'paused'):
        return 'stopped'
    return None


def handle_container_state(ns, global_flags=None, podman_path: str = 'podman'):
    """Returns ``"run"``, ``"attach"``, ``"replace"``, or ``None`` (exit).

    Reads from *ns*: ``run.name``, ``run.auto_attach``, ``run.auto_replace``.
    """
    name = ns.get('run.name')
    if not name:
        return 'run'

    state = detect_container_state(name, global_flags=global_flags, podman_path=podman_path)
    if state is None:
        return 'run'

    is_interactive = sys.stdin.isatty()
    auto_attach = ns.get('run.auto_attach')
    auto_replace = ns.get('run.auto_replace')

    if state == 'running':
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

    # Stopped — cannot attach to a non-running container.
    if auto_attach:
        print(
            f'Warning: Cannot auto-attach to container {name!r} in non-running state',
            file=sys.stderr,
        )
    if auto_replace:
        return 'replace'
    if auto_attach is False and auto_replace is False and not is_interactive:
        return None
    if yes_no_prompt('Replace stopped instance?', False, is_interactive):
        return 'replace'
    return None


def query_container_info(
    name: str, global_flags=None, podman_path: str = 'podman',
) -> Tuple[str, str]:
    """Read PODRUN_WORKDIR and PODRUN_OVERLAYS from container env via inspect.

    Returns ``(workdir, overlays)`` where each is ``''`` if not found.
    """
    gf = ' '.join(shlex.quote(f) for f in global_flags) + ' ' if global_flags else ''
    fmt = shlex.quote('{{range .Config.Env}}{{println .}}{{end}}')
    result = run_os_cmd(
        f'{shlex.quote(podman_path)} {gf}inspect --format={fmt} {shlex.quote(name)}'
    )
    workdir = ''
    overlays = ''
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if line.startswith('PODRUN_WORKDIR='):
                workdir = line.split('=', 1)[1]
            elif line.startswith('PODRUN_OVERLAYS='):
                overlays = line.split('=', 1)[1]
    return workdir, overlays


def build_podman_exec_args(
    ns, name: str, container_workdir: str = '',
    trailing_args=None, explicit_command=None,
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
        args.append(f'-e=PODRUN_STTY_INIT=rows {rows} cols {cols}')
    except (ValueError, OSError):
        pass

    args.append(f'-e=ENV={PODRUN_RC_PATH}')

    if ns.get('run.shell'):
        args.append(f'-e=PODRUN_SHELL={ns["run.shell"]}')
    if ns.get('run.login') is not None:
        args.append(f'-e=PODRUN_LOGIN={"1" if ns["run.login"] else "0"}')

    args.append(name)

    # Determine command: explicit_command ('--' args) > trailing_args after image > interactive
    command = explicit_command or (trailing_args[1:] if trailing_args and len(trailing_args) > 1 else [])
    if command:
        args.extend(command)
    else:
        args.append(PODRUN_EXEC_ENTRY_PATH)

    return args


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------


def build_run_command(result: ParseResult, podman_path: str = 'podman') -> List[str]:
    """Build the full ``podman run`` command from a ParseResult."""
    ns = result.ns
    cmd = [podman_path]
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
    cmd.extend(result.trailing_args)

    # Explicit command after '--'
    if result.explicit_command:
        cmd.append('--')
        cmd.extend(result.explicit_command)

    return cmd


def build_overlay_run_command(result: ParseResult, podman_path: str = 'podman') -> Tuple[List[str], List[str]]:
    """Generate entrypoints, build overlay args, and return the full run command.

    Returns ``(cmd, caps_to_drop)`` where *cmd* is the complete
    ``podman run ...`` arg list and *caps_to_drop* is the list of
    capabilities the entrypoint should drop after bootstrap.

    Overlay args are injected into ``ns['run.passthrough_args']`` before
    delegating to :func:`build_run_command`.
    """
    ns = result.ns
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

    # Generate entrypoints and build user overlay args
    if ns.get('run.user_overlay'):
        entrypoint_path = generate_run_entrypoint(ns, caps_to_drop=compute_caps_to_drop(pt))
        rc_path = generate_rc_sh(ns)
        exec_entry_path = generate_exec_entrypoint()
        user_args, caps_to_drop = _user_overlay_args(ns, pt, entrypoint_path, rc_path, exec_entry_path)
        overlay_args.extend(user_args)
        if alt_entrypoint:
            overlay_args.append(f'--env=PODRUN_ALT_ENTRYPOINT={alt_entrypoint}')

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

    # Apply tilde expansion to passthrough and overlay args when user overlay active
    if ns.get('run.user_overlay'):
        pt = _expand_volume_tilde(pt)
        overlay_args = _expand_volume_tilde(overlay_args)

    # Inject overlay args into passthrough
    ns['run.passthrough_args'] = overlay_args + pt

    return build_run_command(result, podman_path), caps_to_drop


def build_passthrough_command(result: ParseResult, podman_path: str = 'podman') -> List[str]:
    """Build a passthrough ``podman <subcommand> ...`` command."""
    ns = result.ns
    cmd = [podman_path]
    cmd.extend(ns.get('podman_global_args') or [])
    cmd.append(ns['subcommand'])
    cmd.extend(result.subcmd_passthrough_args)
    if result.explicit_command:
        cmd.append('--')
        cmd.extend(result.explicit_command)
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
        podman_cmd = f'{shlex.quote(podman_path)} run --help'
        replace_from, replace_to = 'podman run', 'podrun run'
        podrun_parser = build_root_parser()._run_subparser
    else:
        podman_cmd = f'{shlex.quote(podman_path)} --help'
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


def print_version(podman_path='podman'):
    """Print podman and podrun versions."""
    result = run_os_cmd(f'{shlex.quote(podman_path)} --version')
    if result.returncode == 0:
        print(result.stdout.strip())
    print(f'podrun version {__version__}')


# ---------------------------------------------------------------------------
# Podman help scraper
# ---------------------------------------------------------------------------


def _scrape_podman_help(podman_path, subcmd=None):
    """Scrape ``podman [subcmd] --help`` and return (value_flags, bool_flags, subcommands).

    *value_flags*: flags that take an argument (e.g. ``--env``, ``-e``).
    *bool_flags*: flags with no argument (e.g. ``--rm``, ``--help``).
    *subcommands*: subcommand names from the "Available Commands" section.

    Returns ``None`` on failure.
    """
    cmd_parts = [shlex.quote(podman_path)]
    if subcmd:
        cmd_parts.append(subcmd)
    cmd_parts.append('--help')
    result = run_os_cmd(' '.join(cmd_parts))
    if result.returncode != 0:
        return None

    value_flags = set()
    bool_flags = set()
    subcommands = set()
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
            bucket = value_flags if m.group('val_type') else bool_flags
            bucket.add(m.group('long'))
            if m.group('short'):
                bucket.add(m.group('short'))

    return value_flags, bool_flags, subcommands



# ---------------------------------------------------------------------------
# Completion generators (stub — lift from podrun1 in a later phase)
# ---------------------------------------------------------------------------


def print_completion(shell: str) -> None:
    """Print shell completion script and exit.

    Stub for Phase 1.1.  Full completion scripts (bash/zsh/fish) will be
    lifted from podrun1 in a later phase.
    """
    print(f'# TODO: {shell} completion for podrun — lift from podrun1')
    sys.exit(0)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv=None):
    raw = argv if argv is not None else sys.argv[1:]

    podman_path = shutil.which('podman') or 'podman'

    # Podman-not-found guard — fail early before parsing.
    if get_podman_version(podman_path) is None:
        print(f'Error: podman not found or not functional ({podman_path})', file=sys.stderr)
        sys.exit(1)

    result = parse_args(raw)
    ns = result.ns

    # Immediate-exit flags
    if ns['root.version']:
        print_version(podman_path)
        sys.exit(0)
    if ns['root.completion']:
        print_completion(ns['root.completion'])

    # Help — pass the raw argv so print_help can check for --help before --
    print_help(ns['subcommand'], raw, podman_path)

    # Config resolution (three-way merge: CLI > config-script > devcontainer.json)
    result = resolve_config(result)
    ns = result.ns

    # Store resolution (destroy, resolve, info — all handled inside)
    _apply_store(ns, podman_path)

    # Route
    if ns['subcommand'] == 'run':
        cmd = build_run_command(result, podman_path)
        if ns['root.print_cmd']:
            print(shlex.join(cmd))
            sys.exit(0)
        # Phase 2: os.execvpe(cmd[0], cmd, ...)
    else:
        if ns['subcommand'] is not None:
            # Passthrough to podman
            cmd = build_passthrough_command(result, podman_path)
            if ns['root.print_cmd']:
                print(shlex.join(cmd))
                sys.exit(0)
            os.execvpe(podman_path, cmd, os.environ.copy())


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit('\nError: KeyboardInterrupt received')
