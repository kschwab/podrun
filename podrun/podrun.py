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
__version__ = '1.1.0'
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
from typing import Dict, List, Optional, Tuple

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

# fmt: off
# Podman-run flags that take a value argument (not booleans).
# Scraped from podman 4.5.0.  This set grows monotonically -- add new flags
# as podman versions introduce them; removals are extremely rare.
# Use `podrun --check-flags` to diff this set against the installed podman.
# NOTE: flags using --flag=value syntax always work regardless of this set.
# This only matters for the space-separated form (--flag value).
PODMAN_RUN_VALUE_FLAGS = frozenset({
    '-a', '--attach',
    '-c', '--cpu-shares',
    '-e', '--env',
    '-h', '--hostname',
    '-l', '--label',
    '-m', '--memory',
    '-p', '--publish',
    '-u', '--user',
    '-v', '--volume',
    '-w', '--workdir',
    '--add-host',
    '--annotation',
    '--arch',
    '--authfile',
    '--blkio-weight',
    '--blkio-weight-device',
    '--cap-add',
    '--cap-drop',
    '--cgroup-conf',
    '--cgroup-parent',
    '--cgroupns',
    '--cgroups',
    '--chrootdirs',
    '--cidfile',
    '--conmon-pidfile',
    '--cpu-period',
    '--cpu-quota',
    '--cpu-rt-period',
    '--cpu-rt-runtime',
    '--cpus',
    '--cpuset-cpus',
    '--cpuset-mems',
    '--decryption-key',
    '--detach-keys',
    '--device',
    '--device-cgroup-rule',
    '--device-read-bps',
    '--device-read-iops',
    '--device-write-bps',
    '--device-write-iops',
    '--dns',
    '--dns-option',
    '--dns-search',
    '--entrypoint',
    '--env-file',
    '--env-merge',
    '--expose',
    '--gidmap',
    '--group-add',
    '--group-entry',
    '--health-cmd',
    '--health-interval',
    '--health-on-failure',
    '--health-retries',
    '--health-start-period',
    '--health-startup-cmd',
    '--health-startup-interval',
    '--health-startup-retries',
    '--health-startup-success',
    '--health-startup-timeout',
    '--health-timeout',
    '--hostuser',
    '--image-volume',
    '--init-path',
    '--ip',
    '--ip6',
    '--ipc',
    '--label-file',
    '--log-driver',
    '--log-opt',
    '--mac-address',
    '--memory-reservation',
    '--memory-swap',
    '--memory-swappiness',
    '--mount',
    '--name',
    '--network',
    '--network-alias',
    '--oom-score-adj',
    '--os',
    '--passwd-entry',
    '--personality',
    '--pid',
    '--pidfile',
    '--pids-limit',
    '--platform',
    '--pod',
    '--pod-id-file',
    '--preserve-fds',
    '--pull',
    '--requires',
    '--restart',
    '--sdnotify',
    '--seccomp-policy',
    '--secret',
    '--security-opt',
    '--shm-size',
    '--shm-size-systemd',
    '--stop-signal',
    '--stop-timeout',
    '--subgidname',
    '--subuidname',
    '--sysctl',
    '--systemd',
    '--timeout',
    '--tmpfs',
    '--tz',
    '--uidmap',
    '--ulimit',
    '--umask',
    '--unsetenv',
    '--userns',
    '--uts',
    '--variant',
    '--volumes-from',
})

# Known podman subcommands.  Used by _detect_subcommand() to distinguish
# `podrun <subcommand> [args]` from `podrun [podrun-flags] image [cmd]`.
PODMAN_SUBCOMMANDS = frozenset({
    'attach', 'auto-update', 'build', 'buildx', 'commit', 'compose',
    'container', 'cp', 'create', 'diff', 'events', 'exec', 'export', 'farm',
    'generate', 'healthcheck', 'history', 'image', 'images', 'import',
    'info', 'init', 'inspect', 'kill', 'kube', 'load', 'login',
    'logout', 'logs', 'machine', 'manifest', 'mount', 'network',
    'pause', 'play', 'pod', 'port', 'ps', 'pull', 'push', 'rename',
    'restart', 'rm', 'rmi', 'run', 'save', 'search', 'secret', 'start',
    'stats', 'stop', 'system', 'tag', 'top', 'umount', 'unmount',
    'unpause', 'untag', 'update', 'version', 'volume', 'wait',
})

# Podrun-specific subcommands (not forwarded to podman).
_PODRUN_SUBCOMMANDS = frozenset({
    'store',
})

# Podman global flags (before any subcommand) that take a value argument.
# Used by _detect_subcommand() to skip their values when walking argv.
_PODMAN_GLOBAL_VALUE_FLAGS = frozenset({
    '--cgroup-manager', '--conmon-path', '--connection', '--db-backend',
    '--events-backend', '--identity', '--imagestore', '--log-level',
    '--module', '--network-cmd-path', '--network-config-dir', '--out',
    '--root', '--runroot', '--runtime', '--runtime-flag', '--ssh',
    '--storage-driver', '--storage-opt', '--tmpdir', '--url',
    '--volumepath',
})
# fmt: on

# Config field → PODRUN_OVERLAYS token mapping for _env_args().
_OVERLAY_FIELDS = [
    ('user_overlay', 'user'),
    ('host_overlay', 'host'),
    ('interactive_overlay', 'interactive'),
    ('workspace', 'workspace'),
    ('adhoc', 'adhoc'),
]


@dataclasses.dataclass
class Config:
    image: Optional[str] = None
    name: Optional[str] = None
    user_overlay: bool = False
    host_overlay: bool = False
    interactive_overlay: bool = False
    workspace: bool = False
    adhoc: bool = False
    workspace_folder: str = '/app'
    workspace_mount_src: str = ''
    x11: bool = False
    dood: bool = False
    shell: Optional[str] = None
    login: Optional[bool] = None
    prompt_banner: Optional[str] = None
    auto_attach: bool = False
    auto_replace: bool = False
    print_cmd: bool = False
    command: List[str] = dataclasses.field(default_factory=list)
    container_env: Dict[str, str] = dataclasses.field(default_factory=dict)
    remote_env: Dict[str, str] = dataclasses.field(default_factory=dict)
    podman_args: List[str] = dataclasses.field(default_factory=list)
    bootstrap_caps: List[str] = dataclasses.field(default_factory=list)
    passthrough_args: List[str] = dataclasses.field(default_factory=list)
    exports: List[str] = dataclasses.field(default_factory=list)
    podman_path: Optional[str] = dataclasses.field(default_factory=lambda: shutil.which('podman'))
    fuse_overlayfs: bool = False

    def resolve(self):
        """Sort set-like lists so downstream output is deterministic.

        Only lists whose elements are order-independent are sorted.
        ``command``, ``podman_args``, and ``passthrough_args`` are
        positional and left as-is.
        """
        self.bootstrap_caps.sort()
        self.exports.sort()
        return self


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def _parse_export(entry: str):
    """Parse an export entry into (container_path, host_path, copy_only).

    Accepted forms:
        container_path:host_path        — strict (rm + symlink, fails if rm fails)
        container_path:host_path:0      — copy-only (populate host dir, skip rm/symlink)
    """
    parts = entry.split(':')
    if len(parts) == 3 and parts[2] == '0':
        return parts[0], parts[1], True
    if len(parts) == 2:
        return parts[0], parts[1], False
    raise ValueError(f'Invalid export spec {entry!r}: expected SRC:DST or SRC:DST:0')


def run_os_cmd(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=True,
        universal_newlines=True,
    )


def yes_no_prompt(prompt_msg: str, answer_default: bool, is_interactive: bool) -> bool:
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


def _detect_subcommand(argv):
    """Detect the podman subcommand in *argv*, if any.

    Returns ``(subcmd, index)`` where *subcmd* is the subcommand string and
    *index* is its position in *argv*.  Returns ``(None, 0)`` when no known
    subcommand is found (implicit ``run``).
    """
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == '--':
            break
        if arg.startswith('-'):
            # Check if this global flag takes a space-separated value
            flag_name = arg.split('=', 1)[0]
            if flag_name in _PODMAN_GLOBAL_VALUE_FLAGS and '=' not in arg:
                i += 1  # skip the value
            i += 1
            continue
        # First non-flag argument
        if arg in PODMAN_SUBCOMMANDS or arg in _PODRUN_SUBCOMMANDS:
            return (arg, i)
        return (None, 0)
    return (None, 0)


def _print_version(podman_path):
    """Print podman and podrun versions."""
    result = run_os_cmd(f'{shlex.quote(podman_path)} --version')
    if result.returncode == 0:
        print(result.stdout.strip())
    print(f'podrun {__version__}')


# ---------------------------------------------------------------------------
# devcontainer.json discovery and parsing
# ---------------------------------------------------------------------------


def find_devcontainer_json(start_dir=None):
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
    if path is None:
        return {}
    p = pathlib.Path(path)
    if p.is_dir():
        # Check for devcontainer.json directly inside the given directory first,
        # then fall back to full spec discovery rooted at that directory.
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
    result: dict = devcontainer.get('customizations', {}).get('podrun', {})
    return result


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


class _PodrunParser:
    """Composition wrapper around ``argparse.ArgumentParser``.

    Intercepts ``add_argument`` and ``add_subparsers`` to build a
    class-level registry of flags per command path.  This lets the
    completion generators read flag lists directly instead of
    hardcoding them.

    External code should use the ``@classmethod`` accessors
    (``get_flags``, ``get_value_flags``, ``get_subcommands``,
    ``top_level_subcommands``, ``nested_subcommand_flags``) rather
    than touching ``_registry`` directly.
    """

    _registry: Dict[Optional[str], Dict[str, list]] = {}
    _parsers: Dict[Optional[str], '_PodrunParser'] = {}

    def __init__(self, parser=None, cmd_path=None, **kwargs):
        self.parser = parser or argparse.ArgumentParser(**kwargs)
        self._cmd_path = cmd_path
        self._group = self.parser.add_argument_group('Options')
        # When add_help=False was set, argparse omitted -h/--help from its
        # default group.  Re-add it to our Options group for subcommand
        # parsers (cmd_path is not None).  The root parser (cmd_path=None)
        # handles help externally via _print_help().
        if not self.parser.add_help and cmd_path is not None:
            self._group.add_argument(
                '-h',
                '--help',
                action='help',
                help='show this help message and exit',
            )
        _PodrunParser._registry[cmd_path] = {'flags': [], 'value_flags': [], 'subcommands': []}
        _PodrunParser._parsers[cmd_path] = self

    def add_argument(self, *args, **kwargs):
        target = self._group
        action = target.add_argument(*args, **kwargs)
        if action.option_strings:
            self._registry[self._cmd_path]['flags'].extend(action.option_strings)
            if action.nargs != 0:
                self._registry[self._cmd_path]['value_flags'].extend(action.option_strings)
        return action

    def add_mutually_exclusive_group(self, **kwargs):
        group = self._group.add_mutually_exclusive_group(**kwargs)
        return _PodrunMutuallyExclusiveGroup(group, self._cmd_path)

    def add_subparsers(self, *args, **kwargs):
        kwargs.setdefault('title', 'Available Commands')
        return _PodrunSubParsers(self.parser.add_subparsers(*args, **kwargs), self._cmd_path)

    def parse_args(self, *args, **kwargs):
        return self.parser.parse_args(*args, **kwargs)

    def parse_known_args(self, *args, **kwargs):
        return self.parser.parse_known_args(*args, **kwargs)

    def format_help(self):
        return self.parser.format_help()

    def print_help(self, *args, **kwargs):
        return self.parser.print_help(*args, **kwargs)

    @classmethod
    def get_flags(cls, cmd_path=None) -> List[str]:
        """Return all flag strings registered for *cmd_path*."""
        return list(cls._registry.get(cmd_path, {}).get('flags', []))

    @classmethod
    def get_value_flags(cls, cmd_path=None) -> List[str]:
        """Return flags that take a value argument for *cmd_path*."""
        return list(cls._registry.get(cmd_path, {}).get('value_flags', []))

    @classmethod
    def get_subcommands(cls, cmd_path=None) -> List[str]:
        """Return subcommand names registered under *cmd_path*."""
        return list(cls._registry.get(cmd_path, {}).get('subcommands', []))

    @classmethod
    def top_level_subcommands(cls) -> List[str]:
        """Return sorted list of top-level podrun subcommand names.

        These are registry keys that are not ``None`` and contain no
        spaces (i.e. direct children, not nested paths like
        ``'store init'``).
        """
        return sorted(k for k in cls._registry if k is not None and ' ' not in k)

    @classmethod
    def nested_subcommand_flags(cls) -> Dict[str, Dict[str, List[str]]]:
        """Return ``{parent: {child: [flags]}}`` for nested subcommands.

        Iterates registry keys containing a space (e.g. ``'store init'``)
        and groups them by parent.
        """
        result: Dict[str, Dict[str, List[str]]] = {}
        for key in sorted(cls._registry, key=lambda x: x or ''):
            if key is None or ' ' not in key:
                continue
            parent, child = key.split(' ', 1)
            result.setdefault(parent, {})[child] = list(cls._registry[key].get('flags', []))
        return result

    @classmethod
    def get_parser(cls, cmd_path=None) -> Optional['_PodrunParser']:
        """Return the ``_PodrunParser`` instance registered for *cmd_path*."""
        return cls._parsers.get(cmd_path)


class _PodrunMutuallyExclusiveGroup:
    """Wrapper around argparse mutually exclusive group that tracks flags in the registry."""

    def __init__(self, group, cmd_path):
        self._group = group
        self._cmd_path = cmd_path

    def add_argument(self, *args, **kwargs):
        action = self._group.add_argument(*args, **kwargs)
        if action.option_strings:
            _PodrunParser._registry[self._cmd_path]['flags'].extend(action.option_strings)
            if action.nargs != 0:
                _PodrunParser._registry[self._cmd_path]['value_flags'].extend(action.option_strings)
        return action


class _PodrunSubParsers:
    """Wrapper around argparse subparsers that tracks child parsers in the registry."""

    def __init__(self, sub_parser, parent_path):
        self._sub_parser = sub_parser
        self._parent_path = parent_path

    def add_parser(self, name, *args, **kwargs):
        kwargs.setdefault('add_help', False)
        parser = self._sub_parser.add_parser(name, *args, **kwargs)
        child_path = f'{self._parent_path} {name}'.strip() if self._parent_path else name
        _PodrunParser._registry[self._parent_path]['subcommands'].append(name)
        return _PodrunParser(parser=parser, cmd_path=child_path)


def _print_help(subcmd, argv, parser, podman_path):
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
        result = run_os_cmd(f'{shlex.quote(podman_path)} run --help')
        podman_help = result.stdout.rstrip() if result.returncode == 0 else ''
        podrun_help = parser.format_help()
        # Drop the usage line (first paragraph), keep description + options
        sections = podrun_help.split('\n\n', 1)
        body = sections[-1] if len(sections) > 1 else podrun_help
        print(podman_help.replace('podman run', 'podrun'))
        print()
        print('Podrun:')
        print()
        print(body)
    else:
        # Top-level help (subcmd is None)
        result = run_os_cmd(f'{shlex.quote(podman_path)} --help')
        if result.returncode == 0:
            print(result.stdout.rstrip().replace('podman', 'podrun'))
        else:
            print('podrun - a transparent podman proxy with overlay extensions')
        print()
        print('Podrun:')
        print()
        print('Additional commands and options.')
        print()
        subcmds = _PodrunParser.top_level_subcommands()
        print('Available Commands:')
        print(f'  {{{",".join(subcmds)}}}')
        for name in subcmds:
            p = _PodrunParser.get_parser(name)
            desc = p.parser.description or '' if p else ''
            print(f'    {name:<10}{desc}')
        print()
        print('Run Options:')
        print("  Podrun extends 'podrun run' with overlay flags for host identity mapping,")
        print("  interactive sessions, and more. Run 'podrun run --help' for details.")
        print()

    sys.exit(0)


def _scrape_podman_value_flags(podman_path='podman'):
    """Scrape podman run --help for flags that take a value (not booleans)."""
    result = run_os_cmd(f'{shlex.quote(podman_path)} run --help')
    if result.returncode != 0:
        return None
    value_flags = set()
    for line in result.stdout.splitlines():
        m = re.match(
            r'\s*(?P<short>-\w)?,?\s*(?P<long>--[^\s]+)\s+(?P<val_type>[^\s]+)?\s{2,}(?P<help>\w+.*)',
            line,
        )
        if m and m.group('val_type'):
            value_flags.add(m.group('long'))
            if m.group('short'):
                value_flags.add(m.group('short'))
    return value_flags


def check_flags(podman_path='podman'):
    """Compare PODMAN_RUN_VALUE_FLAGS against the installed podman and report."""
    version = run_os_cmd(f'{shlex.quote(podman_path)} --version')
    if version.returncode != 0:
        print('Error: Could not run podman --version', file=sys.stderr)
        sys.exit(1)
    print(version.stdout.strip())

    scraped = _scrape_podman_value_flags(podman_path=podman_path)
    if scraped is None:
        print('Error: Could not scrape podman run --help', file=sys.stderr)
        sys.exit(1)

    static = PODMAN_RUN_VALUE_FLAGS
    added = sorted(scraped - static)
    removed = sorted(static - scraped)

    print(f'Static set:  {len(static)} flags')
    print(f'Scraped set: {len(scraped)} flags')

    if not added and not removed:
        print('Sets match -- no update needed.')
        sys.exit(0)

    if added:
        print(f'\nMissing from static set ({len(added)} flag(s) to add):')
        for flag in added:
            print(f'  {flag}')
    if removed:
        print(f'\nExtra in static set ({len(removed)} flag(s) possibly removed):')
        for flag in removed:
            print(f'  {flag}')

    sys.exit(1)


def _print_completion(shell: str) -> None:
    """Print shell completion script and exit."""
    if shell == 'bash':
        print(_generate_bash_completion())
    elif shell == 'zsh':
        print(_generate_zsh_completion())
    elif shell == 'fish':
        print(_generate_fish_completion())
    sys.exit(0)


def _completion_data():
    """Extract completion metadata from ``_PodrunParser`` for script generation.

    Returns a dict with pre-joined strings ready for interpolation into
    shell completion templates.
    """
    flags = _PodrunParser.get_flags()
    value_flags = _PodrunParser.get_value_flags()
    top_subcmds = sorted(
        set(_PodrunParser.get_subcommands() + _PodrunParser.top_level_subcommands())
    )
    nested = _PodrunParser.nested_subcommand_flags()

    # Per top-level subcommand: its direct subcommand names
    subcmd_children: Dict[str, List[str]] = {}
    for name in top_subcmds:
        subcmd_children[name] = _PodrunParser.get_subcommands(name)

    return {
        'flags': flags,
        'value_flags': value_flags,
        'flags_str': ' '.join(flags),
        'value_flags_str': ' '.join(value_flags),
        'subcmds_str': ' '.join(top_subcmds),
        'subcmd_children': subcmd_children,
        'nested': nested,
    }


def _generate_bash_completion() -> str:
    """Return a bash completion script that wraps podman's Cobra completions."""
    cd = _completion_data()
    flags_str = cd['flags_str']
    value_flags_str = cd['value_flags_str']
    podrun_subcmds_str = cd['subcmds_str']
    store_subcmds_str = ' '.join(cd['subcmd_children'].get('store', []))

    # Build per-subcommand flag maps for nested subcommands (e.g., store init)
    sub_flag_cases = []
    for parent, children in sorted(cd['nested'].items()):
        for child, child_flags in sorted(children.items()):
            sub_flag_cases.append(
                f'                    "{child}") sub_flags="{" ".join(child_flags)}" ;;'
            )
    sub_flag_case_block = '\n'.join(sub_flag_cases)

    return textwrap.dedent(f"""\
        _podrun() {{
            local cur="${{COMP_WORDS[COMP_CWORD]}}"
            local podrun_flags="{flags_str}"
            local podrun_value_flags="{value_flags_str}"
            local podrun_subcommands="{podrun_subcmds_str}"

            # Detect podrun subcommand context
            local podrun_subcmd=""
            local i=1
            while [ $i -lt $COMP_CWORD ]; do
                local word="${{COMP_WORDS[$i]}}"
                if [[ "$word" != -* ]]; then
                    for ps in $podrun_subcommands; do
                        if [ "$word" = "$ps" ]; then
                            podrun_subcmd="$word"
                            break 2
                        fi
                    done
                    break
                fi
                # Skip value for podrun value flags (space-separated form)
                if [[ "$word" != *=* ]]; then
                    for vf in $podrun_value_flags; do
                        if [ "$word" = "$vf" ]; then
                            i=$((i + 1))
                            break
                        fi
                    done
                fi
                i=$((i + 1))
            done

            if [ -n "$podrun_subcmd" ]; then
                # Podrun subcommand context — complete from registry
                local sub_subcmds=""
                local sub_flags=""
                case "$podrun_subcmd" in
                    "store") sub_subcmds="{store_subcmds_str}" ;;
                esac

                # Detect nested subcommand for flag completion
                local sub_subcmd=""
                local j=$((i + 1))
                while [ $j -lt $COMP_CWORD ]; do
                    local sw="${{COMP_WORDS[$j]}}"
                    if [[ "$sw" != -* ]]; then
                        sub_subcmd="$sw"
                        break
                    fi
                    j=$((j + 1))
                done
                if [ -n "$sub_subcmd" ]; then
                    case "$sub_subcmd" in
{sub_flag_case_block}
                    esac
                fi

                mapfile -t COMPREPLY < <(compgen -W "$sub_subcmds $sub_flags" -- "$cur")
                return
            fi

            # Build filtered args for podman, stripping podrun-only flags
            local args=()
            local has_subcmd=false
            i=1
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
    podrun_subcmds_str = cd['subcmds_str']
    store_subcmds_str = ' '.join(cd['subcmd_children'].get('store', []))

    # Build per-subcommand flag maps for nested subcommands
    sub_flag_cases = []
    for parent, children in sorted(cd['nested'].items()):
        for child, child_flags in sorted(children.items()):
            sub_flag_cases.append(
                f'                    "{child}") sub_flags=({" ".join(child_flags)}) ;;'
            )
    sub_flag_case_block = '\n'.join(sub_flag_cases)

    return textwrap.dedent(f"""\
        #compdef podrun

        _podrun() {{
            local podrun_flags=({flags_str})
            local podrun_value_flags=({value_flags_str})
            local podrun_subcommands=({podrun_subcmds_str})

            # Detect podrun subcommand context
            local podrun_subcmd=""
            local i=2
            while (( i < CURRENT )); do
                local word="${{words[$i]}}"
                if [[ "$word" != -* ]]; then
                    for ps in "${{podrun_subcommands[@]}}"; do
                        if [[ "$word" = "$ps" ]]; then
                            podrun_subcmd="$word"
                            break 2
                        fi
                    done
                    break
                fi
                if [[ "$word" != *=* ]]; then
                    for vf in "${{podrun_value_flags[@]}}"; do
                        if [[ "$word" = "$vf" ]]; then
                            (( i++ ))
                            break
                        fi
                    done
                fi
                (( i++ ))
            done

            if [[ -n "$podrun_subcmd" ]]; then
                local -a sub_subcmds
                local -a sub_flags
                case "$podrun_subcmd" in
                    "store") sub_subcmds=({store_subcmds_str}) ;;
                esac

                # Detect nested subcommand for flag completion
                local sub_subcmd=""
                local j=$((i + 1))
                while (( j < CURRENT )); do
                    local sw="${{words[$j]}}"
                    if [[ "$sw" != -* ]]; then
                        sub_subcmd="$sw"
                        break
                    fi
                    (( j++ ))
                done
                if [[ -n "$sub_subcmd" ]]; then
                    case "$sub_subcmd" in
{sub_flag_case_block}
                    esac
                fi

                local -a descriptions
                for sc in "${{sub_subcmds[@]}}"; do
                    descriptions+=("$sc:podrun subcommand")
                done
                for sf in "${{sub_flags[@]}}"; do
                    descriptions+=("$sf:podrun option")
                done
                _describe 'completions' descriptions
                return
            fi

            # Build filtered args for podman, stripping podrun-only flags
            local args=()
            local has_subcmd=false
            i=2
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
    podrun_subcmds_str = cd['subcmds_str']
    store_subcmds_str = ' '.join(cd['subcmd_children'].get('store', []))

    # Build per-subcommand flag maps for nested subcommands
    sub_flag_cases = []
    for parent, children in sorted(cd['nested'].items()):
        for child, child_flags in sorted(children.items()):
            sub_flag_cases.append(
                f'                    case "{child}"\n'
                f'                        set sub_flags {" ".join(child_flags)}'
            )
    sub_flag_case_block = '\n'.join(sub_flag_cases)

    return textwrap.dedent(f"""\
        function __podrun_complete
            set -l cmdline (commandline -opc)
            set -l cur (commandline -ct)

            set -l podrun_flags {flags_str}
            set -l podrun_value_flags {value_flags_str}
            set -l podrun_subcommands {podrun_subcmds_str}

            # Detect podrun subcommand context
            set -l podrun_subcmd ""
            set -l subcmd_idx 0
            set -l skip_next false
            for i in (seq 2 (count $cmdline))
                if test "$skip_next" = true
                    set skip_next false
                    continue
                end
                set -l word $cmdline[$i]
                if not string match -q '-*' -- $word
                    for ps in $podrun_subcommands
                        if test "$word" = "$ps"
                            set podrun_subcmd $word
                            set subcmd_idx $i
                            break
                        end
                    end
                    if test -n "$podrun_subcmd"
                        break
                    end
                    break
                end
                if not string match -q '*=*' -- $word
                    for vf in $podrun_value_flags
                        if test "$word" = "$vf"
                            set skip_next true
                            break
                        end
                    end
                end
            end

            if test -n "$podrun_subcmd"
                set -l sub_subcmds
                set -l sub_flags
                switch "$podrun_subcmd"
                    case "store"
                        set sub_subcmds {store_subcmds_str}
                end

                # Detect nested subcommand for flag completion
                set -l sub_subcmd ""
                for j in (seq (math $subcmd_idx + 1) (count $cmdline))
                    set -l sw $cmdline[$j]
                    if not string match -q '-*' -- $sw
                        set sub_subcmd $sw
                        break
                    end
                end
                if test -n "$sub_subcmd"
                    switch "$sub_subcmd"
{sub_flag_case_block}
                    end
                end

                for sc in $sub_subcmds
                    echo -e "$sc\\tpodrun subcommand"
                end
                for sf in $sub_flags
                    echo -e "$sf\\tpodrun option"
                end
                return
            end

            # Build filtered args for podman, stripping podrun-only flags
            set -l args
            set -l has_subcmd false
            set skip_next false
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


# ---------------------------------------------------------------------------
# Project-local podrun store
# ---------------------------------------------------------------------------

_PODRUN_STORES_DIR = '/tmp/podrun-stores'


def _runroot_path(graphroot: str) -> str:
    """Return a deterministic runroot path under ``/tmp`` for *graphroot*.

    Uses a truncated SHA-256 hash so the path stays short (well within
    the 108-byte ``sun_path`` limit) and is unique per graphroot.
    """
    h = hashlib.sha256(graphroot.encode()).hexdigest()[:12]
    return f'{_PODRUN_STORES_DIR}/{h}'


def _generate_store_activate(store_dir, bin_dir, runroot_target, registries_conf=None):
    """Write a POSIX sh activate script to *store_dir* ``/activate``.

    The script prepends *bin_dir* to ``PATH``, amends ``PS1``, and
    optionally sets ``CONTAINERS_REGISTRIES_CONF``.  A companion
    ``deactivate_podrun_store`` function undoes everything.
    """
    store_dir = str(store_dir)
    bin_dir = str(bin_dir)
    runroot_target = str(runroot_target)

    lines = [
        '# Podrun local store activation script.',
        '# Generated by podrun — do not edit.',
        '#',
        '# Usage: source .podrun-store/activate',
        '',
        '# Ensure runroot target exists (may be cleared on reboot)',
        f'mkdir -p "{runroot_target}"',
        '',
        'deactivate_podrun_store () {',
        '    PATH="$_PODRUN_STORE_OLD_PATH"',
        '    export PATH',
        '    if [ -n "${_PODRUN_STORE_OLD_PS1+x}" ]; then',
        '        PS1="$_PODRUN_STORE_OLD_PS1"',
        '        export PS1',
        '    fi',
    ]
    if registries_conf:
        lines += [
            '    if [ -n "${_PODRUN_STORE_OLD_REGISTRIES_CONF+x}" ]; then',
            '        CONTAINERS_REGISTRIES_CONF="$_PODRUN_STORE_OLD_REGISTRIES_CONF"',
            '        export CONTAINERS_REGISTRIES_CONF',
            '        unset _PODRUN_STORE_OLD_REGISTRIES_CONF',
            '    else',
            '        unset CONTAINERS_REGISTRIES_CONF',
            '    fi',
        ]
    lines += [
        '    # Remove shell completion',
        '    # shellcheck disable=SC3044',
        '    if [ -n "${BASH_VERSION:-}" ]; then',
        '        complete -r podrun 2>/dev/null',
        '    elif [ -n "${ZSH_VERSION:-}" ]; then',
        '        compdef -d podrun 2>/dev/null',
        '    fi',
        '    unset _PODRUN_STORE_OLD_PATH',
        '    unset _PODRUN_STORE_OLD_PS1',
        '    unset -f deactivate_podrun_store',
        '}',
        '',
        '# Save current values',
        '_PODRUN_STORE_OLD_PATH="$PATH"',
        '_PODRUN_STORE_OLD_PS1="${PS1:-}"',
    ]
    if registries_conf:
        lines += [
            '_PODRUN_STORE_OLD_REGISTRIES_CONF="${CONTAINERS_REGISTRIES_CONF:-}"',
            'if [ -z "${CONTAINERS_REGISTRIES_CONF+x}" ]; then',
            '    unset _PODRUN_STORE_OLD_REGISTRIES_CONF',
            'fi',
        ]
    lines += [
        '',
        '# Activate',
        f'PATH="{bin_dir}:$PATH"',
        'export PATH',
    ]
    if registries_conf:
        lines += [
            f'CONTAINERS_REGISTRIES_CONF="{registries_conf}"',
            'export CONTAINERS_REGISTRIES_CONF',
        ]
    lines += [
        'PS1="(podrun-store) ${PS1:-}"',
        'export PS1',
        '',
        '# Shell completion',
        '# shellcheck disable=SC3044',
        'if [ -n "${BASH_VERSION:-}" ]; then',
        '    eval "$(podrun run --completion bash)"',
        'elif [ -n "${ZSH_VERSION:-}" ]; then',
        '    eval "$(podrun run --completion zsh)"',
        'fi',
        '',
    ]

    pathlib.Path(store_dir, 'activate').write_text('\n'.join(lines) + '\n')


def _warn_missing_subids():
    """Print a note if the current user lacks subuid/subgid ranges."""
    try:
        import getpass

        username = getpass.getuser()
        missing = []
        for path in ('/etc/subuid', '/etc/subgid'):
            try:
                with open(path) as f:
                    if username not in f.read():
                        missing.append(path)
            except FileNotFoundError:
                missing.append(path)
        if missing:
            print(f'\nNote: {username} not found in {" or ".join(missing)}.')
            print('  Podman will show rootless warnings and --userns=keep-id')
            print('  (used by --user-overlay) will not work. To fix:')
            print(
                f'    sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 {username}'
            )
    except Exception:
        pass


def _store_init(args, podman_path):
    """Create a venv-style project-local podrun store."""
    store_dir = pathlib.Path(args.store_dir).resolve()
    graphroot = store_dir / 'graphroot'
    graphroot.mkdir(parents=True, exist_ok=True)

    # Runroot under /tmp (deterministic, short path)
    runroot_target = _runroot_path(str(graphroot))
    pathlib.Path(runroot_target).mkdir(parents=True, exist_ok=True)

    # Symlink store_dir/runroot → /tmp/podrun-stores/<hash>/
    runroot_link = store_dir / 'runroot'
    if runroot_link.is_symlink() or runroot_link.exists():
        runroot_link.unlink()
    runroot_link.symlink_to(runroot_target)

    # bin/ directory with wrapper scripts
    bin_dir = store_dir / 'bin'
    bin_dir.mkdir(parents=True, exist_ok=True)

    storage_driver = args.storage_driver

    # bin/python3 → symlink to sys.executable
    python_link = bin_dir / 'python3'
    if python_link.is_symlink() or python_link.exists():
        python_link.unlink()
    python_link.symlink_to(sys.executable)

    store_flags = (
        f' --root "{graphroot}" --runroot "{runroot_target}" --storage-driver "{storage_driver}"'
    )

    # bin/podman wrapper
    podman_wrapper = bin_dir / 'podman'
    podman_wrapper.write_text(f'#!/bin/sh\nexec "{podman_path}"{store_flags} "$@"\n')
    podman_wrapper.chmod(0o755)

    # bin/podrun wrapper — resolve the path to this script so the wrapper
    # works regardless of whether podrun was invoked as a module or script.
    podrun_script = str(pathlib.Path(__file__).resolve())
    podrun_wrapper = bin_dir / 'podrun'
    podrun_wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{podrun_script}"{store_flags} "$@"\n'
    )
    podrun_wrapper.chmod(0o755)

    # Optional registries.conf
    registries_conf = None
    if args.registry:
        registries_path = store_dir / 'registries.conf'
        registries_path.write_text(
            'unqualified-search-registries = ["docker.io"]\n'
            '\n'
            '[[registry]]\n'
            'prefix = "docker.io"\n'
            'location = "docker.io"\n'
            '\n'
            '[[registry.mirror]]\n'
            f'location = "{args.registry}"\n'
        )
        registries_conf = str(registries_path)

    # activate script
    _generate_store_activate(store_dir, bin_dir, runroot_target, registries_conf=registries_conf)

    _store_print_info(store_dir)

    # Check for subuid/subgid — warn if userns won't work
    _warn_missing_subids()


def _store_print_info(store_dir: pathlib.Path):
    """Print summary information about a podrun store."""
    rel_store = os.path.relpath(store_dir)
    runroot_link = store_dir / 'runroot'
    runroot_target = os.readlink(str(runroot_link)) if runroot_link.is_symlink() else '?'
    runroot_exists = os.path.isdir(runroot_target) if runroot_target != '?' else False

    print(f'Podrun store: {rel_store}')
    print(f'  graphroot:  {rel_store}/graphroot')
    runroot_status = '' if runroot_exists else '  (missing — will be created on activate)'
    print(f'  runroot:    {rel_store}/runroot → {runroot_target}{runroot_status}')
    print(f'  bin:        {rel_store}/bin')

    # Registry config
    registries = store_dir / 'registries.conf'
    if registries.exists():
        print(f'  registries: {rel_store}/registries.conf')

    # Activation status
    store_bin = str(store_dir / 'bin')
    active = store_bin in os.environ.get('PATH', '').split(os.pathsep)
    if active:
        print('\nActivated.')
    else:
        print(f'\nActivate with: source {rel_store}/activate')


def _store_info(args):
    """Print information about an existing podrun store."""
    store_dir = pathlib.Path(args.store_dir).resolve()
    if not store_dir.exists():
        print(f'No store found at {os.path.relpath(store_dir)}.', file=sys.stderr)
        print('Run: podrun store init', file=sys.stderr)
        sys.exit(1)
    _store_print_info(store_dir)


def _store_destroy(args, podman_path):  # noqa: C901 — sequential teardown steps; extraction would fragment a short linear flow
    """Remove a project-local podrun store and its runroot."""
    store_dir = pathlib.Path(args.store_dir).resolve()
    if not store_dir.exists():
        print(f'Error: store directory {store_dir} does not exist', file=sys.stderr)
        sys.exit(1)

    # Read runroot symlink target before removing
    runroot_link = store_dir / 'runroot'
    runroot_target = None
    if runroot_link.is_symlink():
        runroot_target = os.readlink(str(runroot_link))

    # Let podman clean up overlay layers (UID-mapped files) before rm.
    # Reset every graphroot directory found in the store.
    runroot_targets = set()
    if runroot_target:
        runroot_targets.add(runroot_target)
    for gr in sorted(store_dir.glob('graphroot*')):
        if not gr.is_dir():
            continue
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
        shutil.rmtree(str(store_dir))
    except PermissionError:
        subprocess.run(
            [podman_path, 'unshare', 'rm', '-rf', str(store_dir)],
            capture_output=True,
            timeout=120,
        )
        if store_dir.exists():
            print(f'Error: failed to remove {store_dir}', file=sys.stderr)
            sys.exit(1)
    print(f'Removed {store_dir}')

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


def _main_store(argv, parser, podman_path):
    """Handle the ``store`` subcommand."""
    args = parser.parse_args(argv)
    if args.action is None:
        parser.print_help(sys.stderr)
        sys.exit(1)

    if args.action == 'init':
        _store_init(args, podman_path=podman_path)
    elif args.action == 'destroy':
        _store_destroy(args, podman_path=podman_path)
    elif args.action == 'info':
        _store_info(args)


def _expand_config_scripts(argv: list) -> Tuple[List[str], bool]:
    """Expand --config-script=PATH (or --config-script PATH) in argv.

    Runs the script and splices its stdout (shlex-split) into argv at the
    position where --config-script appeared.  Multiple --config-script flags
    are expanded left to right.

    Returns (expanded_argv, had_config_script).
    """
    result = []
    found = False
    past_separator = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == '--':
            past_separator = True

        script_path = None
        if not past_separator:
            if arg.startswith('--config-script='):
                script_path = arg.split('=', 1)[1]
            elif arg == '--config-script' and i + 1 < len(argv):
                i += 1
                script_path = argv[i]

        if script_path is not None:
            found = True
            out = run_os_cmd(f'{shlex.quote(script_path)}')
            if out.returncode != 0:
                print(
                    f'Error: --config-script {script_path} failed '
                    f'(exit {out.returncode}):\n{out.stderr}',
                    file=sys.stderr,
                )
                sys.exit(1)
            result.extend(shlex.split(out.stdout))
        else:
            result.append(arg)
        i += 1
    return result, found


def _build_parser():
    """Build and return the root ``_PodrunParser`` with the full command tree.

    This includes all run flags and the ``store`` subcommand tree.  Called
    once in ``main()`` and passed down to handlers so every consumer shares
    a single parser object and a single ``_PodrunParser`` registry.
    """
    parser = _PodrunParser(
        cmd_path=None,
        prog='podrun',
        description='Additional run options for host identity overlays.',
        add_help=False,
    )
    parser.add_argument('--version', action='version', version=f'podrun {__version__}')
    parser.add_argument('--name', metavar='NAME', help='Container name')
    parser.add_argument(
        '--user-overlay',
        action='store_true',
        default=None,
        help='Map host user identity into container (userns, home directory setup)',
    )
    parser.add_argument(
        '--host-overlay',
        action='store_true',
        default=None,
        help='Overlay host system context (implies --user-overlay; adds network, hostname, workspace, init)',
    )
    parser.add_argument(
        '--interactive-overlay',
        action='store_true',
        default=None,
        help='Interactive overlay (-it, --detach-keys)',
    )
    parser.add_argument(
        '--workspace',
        action='store_true',
        default=None,
        help='Workspace overlay (implies --host-overlay + --interactive-overlay)',
    )
    parser.add_argument(
        '--adhoc',
        action='store_true',
        default=None,
        help='Ad-hoc overlay (implies --workspace + --rm)',
    )
    parser.add_argument(
        '--print-overlays',
        action='store_true',
        default=False,
        help='Print each overlay group and its settings, then exit',
    )
    parser.add_argument('--x11', action='store_true', default=None, help='Enable X11 forwarding')
    parser.add_argument(
        '--dood',
        action='store_true',
        default=None,
        help='Enable Docker-outside-of-Docker (Podman socket)',
    )
    parser.add_argument(
        '--shell',
        metavar='SHELL',
        help='Shell to use inside container (e.g. bash, zsh, /bin/fish)',
    )
    login_group = parser.add_mutually_exclusive_group()
    login_group.add_argument(
        '--login',
        action='store_const',
        const=True,
        default=None,
        dest='login',
        help='Run shell as login shell (sources /etc/profile)',
    )
    login_group.add_argument(
        '--no-login',
        action='store_const',
        const=False,
        dest='login',
        help='Disable login shell',
    )
    parser.add_argument('--prompt-banner', metavar='TEXT', help='Prompt banner text')
    parser.add_argument(
        '--auto-attach',
        action='store_true',
        default=None,
        help='Auto attach to named container if already running',
    )
    parser.add_argument(
        '--auto-replace',
        action='store_true',
        default=None,
        help='Auto replace named container if already running',
    )
    parser.add_argument(
        '--print-cmd',
        '--dry-run',
        action='store_true',
        default=False,
        help='Print the podman command instead of executing it',
    )
    parser.add_argument(
        '--config',
        metavar='PATH',
        help='Explicit path to devcontainer.json',
    )
    parser.add_argument(
        '--no-devconfig',
        action='store_true',
        default=False,
        help='Skip devcontainer.json discovery (--config-script still applies)',
    )
    parser.add_argument(
        '--fuse-overlayfs',
        action='store_true',
        default=None,
        help='Use fuse-overlayfs for overlay mounts (avoids ID-mapped '
        'layer copy on kernels without native overlay idmap support)',
    )
    parser.add_argument(
        '--config-script',
        metavar='PATH',
        help='Run script and inline its stdout as args at this position. '
        'Ordering matters: args after --config-script override the '
        'script output; args before are overridden by it',
    )
    parser.add_argument(
        '--export',
        action='append',
        default=None,
        metavar='SRC:DST[:0]',
        help='Export container path to host (container_path:host_path). '
        'Append :0 for copy-only mode (skip rm/symlink). '
        'Requires --user-overlay. May be repeated.',
    )
    parser.add_argument(
        '--check-flags',
        action='store_true',
        default=False,
        help='Diff static podman value flags against installed podman and exit',
    )
    parser.add_argument(
        '--completion',
        metavar='SHELL',
        choices=['bash', 'zsh', 'fish'],
        help='Generate shell completion script and exit',
    )

    # Store subcommand tree (standalone parser — not an argparse subparser
    # of the root, because parse_known_args rejects positional args that
    # don't match subparser choices).
    store_p = _PodrunParser(
        cmd_path='store',
        prog='podrun store',
        description='Manage project-local podrun stores.',
        add_help=False,
    )
    store_sub = store_p.add_subparsers(dest='action')

    init_p = store_sub.add_parser('init', help='Create a new project-local podrun store')
    init_p.add_argument(
        '--store-dir',
        default='.podrun-store',
        help='Store directory (default: .podrun-store)',
    )
    init_p.add_argument(
        '--registry',
        default=None,
        help='Registry mirror for pulling images',
    )
    init_p.add_argument(
        '--storage-driver',
        default='overlay',
        help='Podman storage driver (default: overlay)',
    )

    destroy_p = store_sub.add_parser('destroy', help='Remove a project-local podrun store')
    destroy_p.add_argument(
        '--store-dir',
        default='.podrun-store',
        help='Store directory (default: .podrun-store)',
    )

    info_p = store_sub.add_parser('info', help='Show information about a podrun store')
    info_p.add_argument(
        '--store-dir',
        default='.podrun-store',
        help='Store directory (default: .podrun-store)',
    )

    return parser


def parse_cli_args(argv=None, parser=None, podman_path='podman'):
    if parser is None:
        parser = _build_parser()

    raw = argv if argv is not None else sys.argv[1:]

    # Inline-expand --config-script: run the script and splice its stdout
    # into argv at the position where --config-script appeared.
    raw, had_config_script = _expand_config_scripts(raw)

    # Optional -- support (still honored if present)
    if '--' in raw:
        idx = raw.index('--')
        flag_section, explicit_command = raw[:idx], raw[idx + 1 :]
    else:
        flag_section, explicit_command = raw, []

    known, unknowns = parser.parse_known_args(flag_section)

    if known.check_flags:
        check_flags(podman_path=podman_path)

    if known.completion:
        _print_completion(known.completion)

    # Use static set to split unknowns into podman flags vs trailing positionals
    podman_value_flags = PODMAN_RUN_VALUE_FLAGS | _PODMAN_GLOBAL_VALUE_FLAGS
    passthrough_flags = []
    trailing = []
    i = 0
    while i < len(unknowns):
        arg = unknowns[i]
        if arg.startswith('-'):
            passthrough_flags.append(arg)
            # If this flag takes a value and doesn't use = syntax, consume next arg too
            flag_name = arg.split('=', 1)[0]
            if flag_name in podman_value_flags and '=' not in arg and i + 1 < len(unknowns):
                i += 1
                passthrough_flags.append(unknowns[i])
        else:
            trailing = unknowns[i:]
            break
        i += 1

    known.passthrough_args = passthrough_flags
    known.trailing_args = trailing
    known.explicit_command = explicit_command
    known.had_config_script = had_config_script
    return known


# ---------------------------------------------------------------------------
# Config merging
# ---------------------------------------------------------------------------


def _expand_volume_tilde(args: list) -> list:
    """Expand ~ in -v/--volume arguments.

    Source (host) ~ expands to USER_HOME.
    Destination (container) ~ expands to /home/{UNAME}.
    This handles the case where volume args come from devcontainer.json
    (no shell expansion) and the container destination path is never
    shell-expanded by podman.
    """
    result = []
    for arg in args:
        # Match -v=src:dest[:opts] or --volume=src:dest[:opts]
        m = re.match(r'^(-v|--volume)=(.*)', arg)
        if not m:
            result.append(arg)
            continue
        flag = m.group(1)
        parts = m.group(2).split(':')
        if len(parts) >= 2:
            parts[0] = re.sub(r'^~', USER_HOME, parts[0])
            parts[1] = re.sub(r'^~', f'/home/{UNAME}', parts[1])
        elif len(parts) == 1:
            parts[0] = re.sub(r'^~', USER_HOME, parts[0])
        result.append(f'{flag}={":".join(parts)}')
    return result


def _resolve_image_and_command(trailing, explicit_command, devcontainer):
    """Deduplicate image from trailing positional args and devcontainer.json."""
    image = devcontainer.get('image')
    if image is None and trailing:
        image = trailing[0]
        command = trailing[1:] + explicit_command
    elif image is not None and trailing and trailing[0] == image:
        # Caller (e.g. devcontainer CLI) passed the same image from json — dedup
        command = trailing[1:] + explicit_command
    else:
        command = trailing + explicit_command
    return image, command


def _resolve_config_script(podrun_cfg, cli_args, podman_args):
    """Run configScript from devcontainer.json and prepend its output to podman_args.

    Skipped when --config-script was used on CLI (already expanded inline).
    """
    config_script = podrun_cfg.get('configScript')
    if config_script and not getattr(cli_args, 'had_config_script', False):
        out = run_os_cmd(f'{shlex.quote(config_script)}')
        if out.returncode == 0:
            podman_args = shlex.split(out.stdout) + podman_args
        else:
            print(
                f'Warning: configScript {config_script} failed (exit {out.returncode})',
                file=sys.stderr,
            )
    return podman_args


def _devcontainer_run_args(devcontainer: dict) -> list:
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


def merge_config(cli_args, podrun_cfg: dict, devcontainer: dict, podman_path=None) -> Config:
    """Merge CLI > customizations.podrun > devcontainer.json top-level."""

    def _first(*values):
        for v in values:
            if v is not None:
                return v
        return None

    trailing = getattr(cli_args, 'trailing_args', [])
    explicit_command = getattr(cli_args, 'explicit_command', [])

    image, command = _resolve_image_and_command(trailing, explicit_command, devcontainer)

    name = _first(cli_args.name, podrun_cfg.get('name'))
    if name is None and image:
        # derive name from image basename
        name = re.sub(r'[/:@]', '-', image.rsplit('/', 1)[-1])

    workspace_folder = devcontainer.get('workspaceFolder', '/app')
    workspace_mount_src = str(pathlib.Path.cwd())

    dc_args = _devcontainer_run_args(devcontainer)
    podman_args = podrun_cfg.get('podmanArgs', [])
    podman_args = _resolve_config_script(podrun_cfg, cli_args, podman_args)
    passthrough_args = getattr(cli_args, 'passthrough_args', [])

    # Dedup: remove dc_args already present in higher-precedence sources
    existing = set(podman_args) | set(passthrough_args)
    dc_args = [a for a in dc_args if a not in existing]

    podman_args = dc_args + podman_args

    # Deduplicate bootstrap caps against user-provided --cap-add args
    # (from both devcontainer podmanArgs and CLI passthrough)
    user_caps = set()
    for arg in (*podman_args, *passthrough_args):
        m = re.match(r'--cap-add=(.*)', arg)
        if m:
            user_caps.add(m.group(1).upper())
    bootstrap_caps = [c for c in BOOTSTRAP_CAPS if c not in user_caps]

    adhoc = _first(cli_args.adhoc, podrun_cfg.get('adhoc'), False)
    workspace = _first(cli_args.workspace, podrun_cfg.get('workspace'), False)
    interactive_overlay = _first(
        cli_args.interactive_overlay, podrun_cfg.get('interactiveOverlay'), False
    )
    host_overlay = _first(cli_args.host_overlay, podrun_cfg.get('hostOverlay'), False)
    user_overlay = _first(cli_args.user_overlay, podrun_cfg.get('userOverlay'), False)

    # Implication chain: adhoc -> workspace, workspace -> host + interactive, host -> user
    if adhoc:
        workspace = True
    if workspace:
        host_overlay = True
        interactive_overlay = True
    if host_overlay:
        user_overlay = True

    # Exports: config provides baseline, CLI appends
    cfg_exports = podrun_cfg.get('exports', [])
    cli_exports = getattr(cli_args, 'export', None) or []
    exports = cfg_exports + cli_exports

    return Config(
        image=image,
        name=name,
        user_overlay=user_overlay,
        host_overlay=host_overlay,
        interactive_overlay=interactive_overlay,
        workspace=workspace,
        adhoc=adhoc,
        workspace_folder=workspace_folder,
        workspace_mount_src=workspace_mount_src,
        shell=_first(cli_args.shell, podrun_cfg.get('shell')),
        x11=_first(cli_args.x11, podrun_cfg.get('x11'), False),
        dood=_first(cli_args.dood, podrun_cfg.get('dood'), False),
        login=_first(cli_args.login, podrun_cfg.get('login')),
        prompt_banner=_first(cli_args.prompt_banner, podrun_cfg.get('promptBanner'), image),
        auto_attach=_first(cli_args.auto_attach, podrun_cfg.get('autoAttach'), False),
        auto_replace=_first(cli_args.auto_replace, podrun_cfg.get('autoReplace'), False),
        print_cmd=cli_args.print_cmd,
        command=command,
        container_env=devcontainer.get('containerEnv', {}),
        remote_env=devcontainer.get('remoteEnv', {}),
        podman_args=podman_args,
        bootstrap_caps=bootstrap_caps,
        passthrough_args=passthrough_args,
        exports=exports,
        podman_path=podman_path or shutil.which('podman'),
        fuse_overlayfs=_first(cli_args.fuse_overlayfs, podrun_cfg.get('fuseOverlayfs'), False),
    )


# ---------------------------------------------------------------------------
# Container state management
# ---------------------------------------------------------------------------


def detect_container_state(
    name: str, global_flags: Optional[List[str]] = None, podman_path: str = 'podman'
):
    """Returns "running", "stopped", or None."""
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


def handle_container_state(  # noqa: C901 — inherent state×config decision tree; extraction would fragment a short sequential flow
    config: Config, global_flags: Optional[List[str]] = None, podman_path: str = 'podman'
):
    """Returns "run", "attach", "start", "replace", or None (exit)."""
    name = config.name
    if not name:
        return 'run'

    state = detect_container_state(name, global_flags=global_flags, podman_path=podman_path)
    if state is None:
        return 'run'

    is_interactive = sys.stdin.isatty()
    auto_attach = config.auto_attach
    auto_replace = config.auto_replace

    if state == 'running':
        if auto_attach:
            return 'attach'
        if auto_replace:
            return 'replace'
        # Both explicitly False -- don't attach or replace
        if auto_attach is False and auto_replace is False:
            return None
        # At least one is None (unset) -- prompt interactively
        if yes_no_prompt('Attach to already running instance?', True, is_interactive):
            return 'attach'
        if yes_no_prompt('Replace already running instance?', False, is_interactive):
            return 'replace'
        return None

    # non-running — cannot attach to a non-running container (no exec target).
    # Warn if auto_attach was requested, then fall through to replace logic.
    if auto_attach:
        print(
            f'Warning: Cannot auto-attach to container {name!r} when in non-running state',
            file=sys.stderr,
        )
    if auto_replace:
        return 'replace'
    # Both explicitly set and non-interactive -- don't replace
    if auto_attach is False and auto_replace is False and not is_interactive:
        return None
    if yes_no_prompt('Replace stopped instance?', False, is_interactive):
        return 'replace'
    return None


# ---------------------------------------------------------------------------
# Entrypoint generation
# ---------------------------------------------------------------------------


def _write_sha_file(content: str, prefix: str, suffix: str) -> str:
    """Write content to a SHA-named file in PODRUN_TMP. Idempotent.

    Different content produces a different SHA filename automatically.
    Old files from previous versions or configs are harmless (unused)
    and are cleaned when the tmpfs is cleared on reboot.
    """
    content_hash = hashlib.sha256(content.encode()).hexdigest()[:12]
    filename = f'{prefix}{content_hash}{suffix}'
    path = os.path.join(PODRUN_TMP, filename)
    if not os.path.exists(path):
        pathlib.Path(PODRUN_TMP).mkdir(parents=True, exist_ok=True)
        with open(path, 'w') as f:
            f.write(content)
        os.chmod(path, 0o755)
    return path


def generate_run_entrypoint(config: Config) -> str:
    """Generate the entrypoint script and return its path (SHA-named, idempotent)."""
    config.resolve()
    login_flag = ' -l' if config.login else ''
    caps_to_drop = config.bootstrap_caps
    default_shell = config.shell

    # Build export blocks
    export_blocks = ''
    if config.exports:
        lines = []
        for entry in config.exports:
            src, _, copy_only = _parse_export(entry)
            staging = f'/.podrun/exports/{hashlib.sha256(src.encode()).hexdigest()[:12]}'
            mode = 'copy' if copy_only else 'mount'
            lines.append(f'        # Export ({mode}): {src}')
            if copy_only:
                # Copy-only: populate staging dir, leave original intact.
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
                # Strict: populate staging, remove original, symlink.
                # set -e ensures rm failure is fatal.
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
          if [ -d /etc/sudoers.d ]; then
            echo "{UNAME} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/podrun 2>/dev/null || true
            chmod 440 /etc/sudoers.d/podrun 2>/dev/null || true
          else
            echo "{UNAME} ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers 2>/dev/null || true
          fi
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


# ---------------------------------------------------------------------------
# RC shell (prompt/banner) generation
# ---------------------------------------------------------------------------


def generate_rc_sh(config: Config) -> str:
    """Generate the rc.sh prompt/banner script and return its path (SHA-named, idempotent)."""
    prompt_banner = config.prompt_banner or 'podrun'
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


def generate_exec_entrypoint(config: Config) -> str:
    """Generate exec-entrypoint.sh and return its path (SHA-named, idempotent).

    The script provides consistent session setup for ``podman exec`` sessions,
    which bypass the container's entrypoint.  It reads ``PODRUN_*`` env vars
    (persisted at ``podman run`` time) and performs shell resolution, SHELL
    export, stty resize, and login mode handling.
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
        # Priority: $1 arg → $PODRUN_SHELL → /etc/passwd → /bin/sh
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
        # Priority: $2 arg → $PODRUN_LOGIN → 0 (no login)
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
# Podman argument builders
# ---------------------------------------------------------------------------


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
    print('  workspace (implies host + interactive):')
    print('    --host-overlay')
    print('    --interactive-overlay')
    print()
    print('  adhoc (implies workspace):')
    print('    --workspace')
    print('    --rm')
    print()


def _parse_image_ref(image: str) -> Tuple[str, str, str]:
    """Break an image reference into ``(registry, name, tag)``.

    Registry defaults to ``docker.io`` and tag defaults to ``latest``
    when not explicitly present (matches Docker/OCI conventions).

    >>> _parse_image_ref('registry.io/org/app:v1')
    ('registry.io', 'org/app', 'v1')
    >>> _parse_image_ref('alpine')
    ('docker.io', 'alpine', 'latest')
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


def _user_overlay_args(config, pt, entrypoint_path, rc_path, exec_entry_path):
    """Build args for --user-overlay: map host user identity into container."""
    args = []
    if not _passthrough_has_flag(pt, '--userns'):
        args.append('--userns=keep-id')
    if not _passthrough_has_flag(pt, '--passwd-entry'):
        args.append(f'--passwd-entry={UNAME}:*:{UID}:{GID}:{UNAME}:/home/{UNAME}:/bin/sh')
    for cap in config.bootstrap_caps:
        args.append(f'--cap-add={cap}')
    args.append(f'--entrypoint={PODRUN_ENTRYPOINT_PATH}')
    args.append(f'-v={entrypoint_path}:{PODRUN_ENTRYPOINT_PATH}:ro')
    args.append(f'-v={rc_path}:{PODRUN_RC_PATH}:ro')
    args.append(f'-v={exec_entry_path}:{PODRUN_EXEC_ENTRY_PATH}:ro')
    args.append(f'--env=ENV={PODRUN_RC_PATH}')
    for entry in config.exports:
        container_path, host_path, _ = _parse_export(entry)
        abs_host = os.path.abspath(host_path)
        os.makedirs(abs_host, exist_ok=True)
        staging_hash = hashlib.sha256(container_path.encode()).hexdigest()[:12]
        args.append(f'-v={abs_host}:/.podrun/exports/{staging_hash}')
    return args


def _interactive_overlay_args(config, pt):
    """Build args for --interactive-overlay: interactive session flags."""
    args = []
    if not (_passthrough_has_short_flag(pt, 'i') or _passthrough_has_short_flag(pt, 't')):
        args.append('-it')
    args.append('--detach-keys=ctrl-q,ctrl-q')
    return args


def _host_overlay_args(config, pt):
    """Build args for --host-overlay: overlay host system context onto container."""
    args = []
    if not _passthrough_has_flag(pt, '--hostname'):
        args.append(f'--hostname={platform.node()}')
    if not _passthrough_has_flag(pt, '--network'):
        args.append('--network=host')
    if not _passthrough_has_exact(pt, '--security-opt=seccomp=unconfined'):
        args.append('--security-opt=seccomp=unconfined')
    if not _passthrough_has_exact(pt, '--init'):
        args.append('--init')
    args.append(f'-v={config.workspace_mount_src}:{config.workspace_folder}')
    if not _passthrough_has_flag(pt, '-w') and not _passthrough_has_flag(pt, '--workdir'):
        args.append(f'-w={config.workspace_folder}')
    if not _passthrough_has_exact(pt, '--env=TERM=xterm-256color'):
        args.append('--env=TERM=xterm-256color')
    return args


def _x11_args(config):
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


def _dood_args(config):
    """Build args for DooD (Docker-outside-of-Docker via rootless Podman socket)."""
    args = []
    podman_socket = f'/run/user/{UID}/podman/podman.sock'
    if pathlib.Path(podman_socket).exists():
        args.append(f'-v={podman_socket}:/run/podman/podman.sock')
    return args


def _env_args(config):
    """Build args for container environment variables and PODRUN_* env vars."""
    args = []
    for key, val in config.container_env.items():
        args.append(f'--env={key}={val}')
    for key, val in config.remote_env.items():
        args.append(f'--env={key}={val}')

    overlays = [name for field, name in _OVERLAY_FIELDS if getattr(config, field)]
    overlay_str = ','.join(overlays) if overlays else 'none'
    args.append(f'--env=PODRUN_OVERLAYS={overlay_str}')

    if config.host_overlay:
        args.append(f'--env=PODRUN_WORKDIR={config.workspace_folder}')
    if config.shell:
        args.append(f'--env=PODRUN_SHELL={config.shell}')
    if config.login is not None:
        args.append(f'--env=PODRUN_LOGIN={"1" if config.login else "0"}')

    repo, name, tag = _parse_image_ref(config.image)
    args.append(f'--env=PODRUN_IMG={config.image}')
    args.append(f'--env=PODRUN_IMG_NAME={name}')
    args.append(f'--env=PODRUN_IMG_REPO={repo}')
    args.append(f'--env=PODRUN_IMG_TAG={tag}')
    return args


def _validate_overlay_args(config):
    """Error on args that conflict with enabled overlays."""
    if not config.user_overlay:
        return
    all_args = [*config.podman_args, *config.passthrough_args]

    # --user=X / -u X conflicts with user-overlay identity mapping
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

    # --userns=X (not keep-id) warns — user may have a reason
    for arg in all_args:
        m = re.match(r'--userns=(.*)', arg)
        if m and m.group(1) != 'keep-id':
            print(
                f"Warning: {arg} overrides --user-overlay's --userns=keep-id.\n"
                'User identity mapping may not work correctly.',
                file=sys.stderr,
            )
            break


def build_podman_args(  # noqa: C901 — linear overlay dispatch; each branch is independent
    config: Config,
    entrypoint_path: Optional[str] = None,
    rc_path: Optional[str] = None,
    exec_entry_path: Optional[str] = None,
) -> List[str]:
    config.resolve()
    _validate_overlay_args(config)
    args = ['run']
    pt = config.passthrough_args

    if config.name:
        args.append(f'--name={config.name}')

    if config.user_overlay:
        args.extend(_user_overlay_args(config, pt, entrypoint_path, rc_path, exec_entry_path))
    if config.interactive_overlay:
        args.extend(_interactive_overlay_args(config, pt))
    if config.host_overlay:
        args.extend(_host_overlay_args(config, pt))
    if config.x11:
        args.extend(_x11_args(config))
    if config.dood:
        args.extend(_dood_args(config))

    if config.adhoc:
        if not _passthrough_has_exact(pt, '--rm') and '--rm' not in config.podman_args:
            args.append('--rm')

    if config.image is None:
        raise ValueError('config.image must be set before building podman args')

    args.extend(_env_args(config))

    # Extra podman args from config
    if config.user_overlay:
        args.extend(_expand_volume_tilde(config.podman_args))
        args.extend(_expand_volume_tilde(config.passthrough_args))
    else:
        args.extend(config.podman_args)
        args.extend(config.passthrough_args)

    # Image
    args.append(config.image)

    # Command
    if config.command:
        args.extend(config.command)

    return args


def query_container_info(
    name: str, global_flags: Optional[List[str]] = None, podman_path: str = 'podman'
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
    config: Config,
    container_workdir: str = '',
) -> List[str]:
    """Build ``podman exec`` args for attaching to a running container.

    Shell resolution, SHELL export, and login handling are delegated to
    ``exec-entrypoint.sh`` inside the container.  CLI overrides are passed as
    ``-e=PODRUN_*`` env vars that exec-entrypoint.sh reads.
    """
    args = ['exec']
    if config.name is None:
        raise ValueError('config.name must be set before building exec args')

    args.append('-it')
    args.append('--detach-keys=ctrl-q,ctrl-q')

    if container_workdir:
        args.append(f'-w={container_workdir}')

    # Pass terminal dimensions for stty resize inside exec-entrypoint.sh
    try:
        cols, rows = shutil.get_terminal_size()
        args.append(f'-e=PODRUN_STTY_INIT=rows {rows} cols {cols}')
    except (ValueError, OSError):
        pass

    # Ensure rc.sh is sourced by POSIX shells on startup (PS1, etc.)
    args.append(f'-e=ENV={PODRUN_RC_PATH}')

    # CLI overrides passed as env vars for exec-entrypoint.sh to read
    if config.shell:
        args.append(f'-e=PODRUN_SHELL={config.shell}')
    if config.login is not None:
        args.append(f'-e=PODRUN_LOGIN={"1" if config.login else "0"}')

    args.append(config.name)

    if config.command:
        # Commands bypass exec-entrypoint.sh and run directly
        args.extend(config.command)
    else:
        # Interactive session: delegate to exec-entrypoint.sh
        args.append(PODRUN_EXEC_ENTRY_PATH)

    return args


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _main_exec(argv, global_flags=None, podman_path='podman'):
    """Passthrough exec to podman."""
    gf = global_flags or []
    os.execvpe(podman_path, [podman_path] + gf + ['exec'] + argv, os.environ.copy())


def _main_run(config, global_flags=None):  # noqa: C901 — linear pipeline with early-exit guards; extracting checks would scatter a cohesive flow
    """Handle the ``run`` subcommand (or implicit run)."""
    gf = global_flags or []

    if not config.image:
        print(
            'Error: No image specified. Pass image as argument or set "image" in devcontainer.json.',
            file=sys.stderr,
        )
        sys.exit(1)

    if config.exports and not config.user_overlay:
        print(
            'Error: --export requires --user-overlay (or an overlay that implies it).',
            file=sys.stderr,
        )
        sys.exit(1)

    # Container state management.
    # --print-cmd allows prompts so the printed command reflects the user's choice.
    # None bypasses the 'is False' guard to reach the prompt path; non-interactive
    # prompts use defaults (attach for running, start for stopped).
    if config.print_cmd and not config.auto_attach and not config.auto_replace:
        config = dataclasses.replace(config, auto_attach=None, auto_replace=None)
    action = handle_container_state(config, global_flags=gf, podman_path=config.podman_path)
    if action is None:
        sys.exit(0)
    pm = shlex.quote(config.podman_path)
    gf_str = ' '.join(shlex.quote(f) for f in gf) + ' ' if gf else ''
    replace_rm_cmd = None
    if action == 'replace':
        replace_rm_cmd = f'{pm} {gf_str}rm -f {shlex.quote(config.name)}'
        if not config.print_cmd:
            run_os_cmd(replace_rm_cmd)
        action = 'run'
    if action == 'attach':
        container_workdir, container_overlays = query_container_info(
            config.name, global_flags=gf, podman_path=config.podman_path
        )
        if 'user' not in container_overlays.split(','):
            print(
                f'Error: container {config.name!r} was not created with podrun user overlay.\n'
                f'Cannot auto-attach: exec-entrypoint.sh is not present in the container.\n'
                f'Use --auto-replace instead to replace the container, or remove it with:\n'
                f'  podman rm {config.name}',
                file=sys.stderr,
            )
            sys.exit(1)
        cmd = (
            [config.podman_path]
            + gf
            + build_podman_exec_args(config, container_workdir=container_workdir)
        )
        if config.print_cmd:
            print(shlex.join(cmd))
            sys.exit(0)
        os.execvpe(config.podman_path, cmd, os.environ.copy())

    entrypoint_path = None
    rc_path = None
    exec_entry_path = None

    if config.user_overlay:
        # Generate entrypoint, rc.sh, and exec-entrypoint.sh with SHA-based filenames (idempotent).
        entrypoint_path = generate_run_entrypoint(config)
        rc_path = generate_rc_sh(config)
        exec_entry_path = generate_exec_entrypoint(config)

        # Clean stale files (>48h) from previous configs.
        run_os_cmd(f'find {PODRUN_TMP} -mtime +1 -delete 2>/dev/null')

    podman_args = build_podman_args(config, entrypoint_path, rc_path, exec_entry_path)

    # fuse-overlayfs: inject --storage-opt when enabled and available.
    if config.fuse_overlayfs:
        fuse_path = shutil.which('fuse-overlayfs')
        if fuse_path:
            gf = gf + ['--storage-opt', f'overlay.mount_program={fuse_path}']
        else:
            print(
                'Error: --fuse-overlayfs requested but fuse-overlayfs not found in PATH',
                file=sys.stderr,
            )
            sys.exit(1)

        # fuse-overlayfs cannot overlay single files — only directories.
        # Convert :O to :ro for file-type volume mounts.
        converted = []
        for arg in podman_args:
            m = re.match(r'^(-v=|--volume=)(.+)$', arg)
            if not m:
                converted.append(arg)
                continue
            prefix, spec = m.group(1), m.group(2)
            parts = spec.split(':')
            if len(parts) >= 3 and parts[-1] == 'O' and os.path.isfile(parts[0]):
                parts[-1] = 'ro'
            converted.append(prefix + ':'.join(parts))
        podman_args = converted

    cmd = [config.podman_path] + gf + podman_args
    if config.print_cmd:
        if replace_rm_cmd:
            print(replace_rm_cmd)
        print(shlex.join(cmd))
        sys.exit(0)
    os.execvpe(config.podman_path, cmd, os.environ.copy())


def _resolve_podman_path(podrun_cfg, default_path):
    """Resolve podman binary path from devcontainer config or default.

    If ``podmanPath`` is specified in *podrun_cfg*, resolve it via
    ``shutil.which`` (handles both bare names and absolute paths).
    Exits with an error if the specified path cannot be found.
    """
    if 'podmanPath' not in podrun_cfg:
        return default_path
    resolved = shutil.which(podrun_cfg['podmanPath'])
    if not resolved:
        print(
            f"Error: podmanPath '{podrun_cfg['podmanPath']}' not found.",
            file=sys.stderr,
        )
        sys.exit(1)
    return resolved


def main(argv=None):
    """Dispatch to the appropriate handler based on the podman subcommand.

    Podman global flags (``--root``, ``--runroot``, etc.) that appear before
    the subcommand are extracted and forwarded into the final ``podman``
    invocation in the correct position (before the subcommand).

    Routing:
      podrun [global] run [args]   → _main_run (enhanced run with overlays)
      podrun [global] exec [args]  → _main_exec (passthrough to podman exec)
      podrun -v                    → _print_version (both podman + podrun)
      podrun [global] version …    → os.execvpe('podman', ...) (passthrough)
      podrun [global] <other> …    → os.execvpe('podman', ...) (passthrough)
    """
    raw = argv if argv is not None else sys.argv[1:]
    parser = _build_parser()

    # Guard: refuse to run inside a podrun container
    if os.environ.get('PODRUN_OVERLAYS'):
        print(
            'Error: podrun cannot be run inside a podrun container.\n'
            'Nested podrun is not supported.',
            file=sys.stderr,
        )
        sys.exit(1)

    config = Config()  # resolve podman_path once

    if not config.podman_path:
        print('Error: podman not found in PATH.', file=sys.stderr)
        sys.exit(1)

    # Special case: bare -v → version (devcontainer CLI isPodman check)
    if raw == ['-v']:
        _print_version(config.podman_path)
        sys.exit(0)

    subcmd, idx = _detect_subcommand(raw)
    global_flags = raw[:idx]
    subcmd_argv = raw[idx + 1 :] if subcmd is not None else raw

    # Consolidated help — handles top-level and run, respects '--'
    _print_help(subcmd, subcmd_argv, parser, config.podman_path)

    if subcmd == 'run':
        # Full config resolution in main() — _main_run receives resolved Config
        cli_args = parse_cli_args(subcmd_argv, parser=parser, podman_path=config.podman_path)

        if cli_args.print_overlays:
            print_overlays()
            sys.exit(0)

        if cli_args.no_devconfig:
            devcontainer = {}
        elif cli_args.config:
            devcontainer = parse_devcontainer_json(pathlib.Path(cli_args.config))
        else:
            devcontainer = parse_devcontainer_json(find_devcontainer_json())

        podrun_cfg = extract_podrun_config(devcontainer)
        podman_path = _resolve_podman_path(podrun_cfg, config.podman_path)
        config = merge_config(cli_args, podrun_cfg, devcontainer, podman_path=podman_path)
        _main_run(config, global_flags=global_flags)
    elif subcmd == 'exec':
        _main_exec(subcmd_argv, global_flags=global_flags, podman_path=config.podman_path)
    elif subcmd == 'store':
        _main_store(
            subcmd_argv,
            parser=parser.get_parser('store'),
            podman_path=config.podman_path,
        )
    else:
        # Other podman subcommand or no subcommand — passthrough
        os.execvpe(config.podman_path, [config.podman_path] + raw, os.environ.copy())


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit('\nError: KeyboardInterrupt received')
