#!/usr/bin/env python3
# Copyright (c) 2026, Kyle Schwab
# All rights reserved.
#
# This source code is licensed under the MIT license found at
# https://github.com/kschwab/podrun/blob/main/LICENSE.md
"""
podrun2 — CLI parsing module
#############################

Phase 1.1: argparse-based CLI parsing for podrun.
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
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from typing import List, Tuple

# Podrun-specific subcommands (not forwarded to podman).
_PODRUN_SUBCOMMANDS = frozenset({
    'store',
})

# Podrun root flags that overlap with podman global flags and are handled
# by the root parser directly (skip registering as passthrough).
_PODRUN_HANDLED_ROOT_FLAGS = frozenset({'--version', '-v'})

# Podrun run flags that overlap with podman run value flags and are handled
# by the run parser directly (skip registering as passthrough).
_PODRUN_HANDLED_RUN_FLAGS = frozenset({'--name'})


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


def expand_config_scripts(argv: list) -> Tuple[List[str], bool]:
    """Stub for Phase 1.1 — returns argv unchanged.

    Phase 1.2 will run the script and splice its stdout into argv.
    """
    return list(argv), False


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
    parser._optionals.title = 'Options'

    # -- Podrun global flags (dest='root_*') ----------------------------------
    parser.add_argument(
        '--print-cmd',
        '--dry-run',
        dest='root.print_cmd',
        action='store_true',
        default=False,
        help='Print the podman command instead of executing it',
    )
    parser.add_argument(
        '--config',
        dest='root.config',
        metavar='PATH',
        help='Explicit path to devcontainer.json',
    )
    parser.add_argument(
        '--config-script',
        dest='root.config_script',
        metavar='PATH',
        help='Run script and inline its stdout as args',
    )
    parser.add_argument(
        '--no-devconfig',
        dest='root.no_devconfig',
        action='store_true',
        default=False,
        help='Skip devcontainer.json discovery',
    )
    parser.add_argument(
        '--completion',
        dest='root.completion',
        metavar='SHELL',
        choices=['bash', 'zsh', 'fish'],
        help='Generate shell completion script and exit',
    )
    parser.add_argument(
        '--version',
        '-v',
        dest='root.version',
        action='store_true',
        default=False,
        help=argparse.SUPPRESS,
    )

    # -- Store-related global flags (with translation) ------------------------
    parser.add_argument(
        '--store',
        dest='root.store',
        metavar='DIR',
        default=None,
        help='Use project-local store directory',
    )
    parser.add_argument(
        '--ignore-store',
        dest='root.ignore_store',
        action='store_true',
        default=False,
        help='Suppress auto-discovery of project-local store',
    )
    parser.add_argument(
        '--auto-init-store',
        dest='root.auto_init_store',
        action='store_true',
        default=False,
        help='Auto-create store if missing (requires --store)',
    )
    parser.add_argument(
        '--store-registry',
        dest='root.store.registry',
        metavar='HOST',
        default=None,
        help='Registry mirror for auto-init',
    )

    # -- Podman global value flags (passthrough) ------------------------------
    for flag in sorted(flags.global_value_flags):
        if flag in _PODRUN_HANDLED_ROOT_FLAGS:
            continue
        parser.add_argument(
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
        parser.add_argument(
            flag,
            action=_PassthroughAction,
            dest='podman_global_args',
            nargs=0,
            help=argparse.SUPPRESS,
        )

    # -- Subparsers for routing -----------------------------------------------
    _podrun_metavar = '{' + ','.join(sorted(_PODRUN_SUBCOMMANDS)) + '}'
    subs = parser.add_subparsers(dest='subcommand', title='Available Commands', metavar=_podrun_metavar)
    subs.required = False  # Allow no subcommand (for --version, --help, etc.)

    # Real subparsers for podrun commands (full flag parsing)
    run_parser = _build_run_subparser(subs, flags.run_value_flags, flags.run_boolean_flags)
    store_parser = _build_store_subparser(subs)

    # Empty subparsers for podman passthrough commands
    for subcmd in sorted(flags.subcommands - {'run'}):
        subs.add_parser(subcmd, add_help=False)

    # Stash for help/completion access
    parser._run_subparser = run_parser
    parser._store_subparser = store_parser

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
    parser._optionals.title = 'Options'

    # -- Podrun run flags (dest='run_*') --------------------------------------
    parser.add_argument('--name', dest='run.name', metavar='NAME', help=argparse.SUPPRESS)
    parser.add_argument(
        '--user-overlay',
        dest='run.user_overlay',
        action='store_true',
        default=None,
        help='Map host user identity into container',
    )
    parser.add_argument(
        '--host-overlay',
        dest='run.host_overlay',
        action='store_true',
        default=None,
        help='Overlay host system context (implies --user-overlay)',
    )
    parser.add_argument(
        '--interactive-overlay',
        dest='run.interactive_overlay',
        action='store_true',
        default=None,
        help='Interactive overlay (-it, --detach-keys)',
    )
    parser.add_argument(
        '--workspace',
        dest='run.workspace',
        action='store_true',
        default=None,
        help='Workspace overlay (implies --host-overlay + --interactive-overlay)',
    )
    parser.add_argument(
        '--adhoc',
        dest='run.adhoc',
        action='store_true',
        default=None,
        help='Ad-hoc overlay (implies --workspace + --rm)',
    )
    parser.add_argument(
        '--print-overlays',
        dest='run.print_overlays',
        action='store_true',
        default=False,
        help='Print each overlay group and its settings, then exit',
    )
    parser.add_argument(
        '--x11',
        dest='run.x11',
        action='store_true',
        default=None,
        help='Enable X11 forwarding',
    )
    parser.add_argument(
        '--podman-remote',
        dest='run.podman_remote',
        action='store_true',
        default=None,
        help='Podman socket passthrough',
    )
    parser.add_argument(
        '--shell', dest='run.shell', metavar='SHELL', help='Shell to use inside container'
    )

    login_group = parser.add_mutually_exclusive_group()
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

    parser.add_argument(
        '--prompt-banner', dest='run.prompt_banner', metavar='TEXT', help='Prompt banner text'
    )
    parser.add_argument(
        '--auto-attach',
        dest='run.auto_attach',
        action='store_true',
        default=None,
        help='Auto attach to named container if already running',
    )
    parser.add_argument(
        '--auto-replace',
        dest='run.auto_replace',
        action='store_true',
        default=None,
        help='Auto replace named container if already running',
    )
    parser.add_argument(
        '--export',
        dest='run.export',
        action='append',
        default=None,
        metavar='SRC:DST[:0]',
        help='Export container path to host. May be repeated.',
    )
    parser.add_argument(
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
        parser.add_argument(
            flag,
            action=_PassthroughAction,
            dest='run.passthrough_args',
            nargs=1,
            help=argparse.SUPPRESS,
        )

    # -- Podman run boolean flags (passthrough, dest='run.passthrough_args') --
    for flag in sorted(run_boolean_flags):
        parser.add_argument(
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


def _build_store_subparser(subs) -> argparse.ArgumentParser:
    """Add ``store`` subparser with init/destroy/info sub-subcommands.

    Store flags use ``dest='store_*'`` prefix.
    """
    parser = subs.add_parser(
        'store',
        description='Manage project-local podrun stores.',
        help='Manage project-local podrun stores',
    )
    parser._optionals.title = 'Options'

    store_subs = parser.add_subparsers(dest='store.action', title='Available Commands')
    store_subs.required = False

    init_p = store_subs.add_parser('init', help='Create a new project-local podrun store')
    init_p.add_argument(
        '--store-dir',
        dest='store.store_dir',
        default='.devcontainer/.podrun/store',
        help='Store directory (default: .devcontainer/.podrun/store)',
    )
    init_p.add_argument(
        '--registry',
        dest='store.registry',
        default=None,
        help='Registry mirror for pulling images',
    )
    init_p.add_argument(
        '--storage-driver',
        dest='store.storage_driver',
        default='overlay',
        help='Podman storage driver (default: overlay)',
    )

    destroy_p = store_subs.add_parser('destroy', help='Remove a project-local podrun store')
    destroy_p.add_argument(
        '--store-dir',
        dest='store.store_dir',
        default='.devcontainer/.podrun/store',
        help='Store directory (default: .devcontainer/.podrun/store)',
    )

    info_p = store_subs.add_parser('info', help='Show information about a podrun store')
    info_p.add_argument(
        '--store-dir',
        dest='store.store_dir',
        default='.devcontainer/.podrun/store',
        help='Store directory (default: .devcontainer/.podrun/store)',
    )

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
    argv, _had_config_script = expand_config_scripts(argv)

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

    elif subcmd == 'store':
        if unknowns:
            root.error(f'unrecognized arguments for store: {" ".join(unknowns)}')

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

    # Passthrough args (podman value + boolean flags)
    cmd.extend(ns.get('run.passthrough_args') or [])

    # Trailing positionals (image + command)
    cmd.extend(result.trailing_args)

    # Explicit command after '--'
    if result.explicit_command:
        cmd.append('--')
        cmd.extend(result.explicit_command)

    return cmd


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


def build_store_command(result: ParseResult, podman_path: str = 'podman') -> List[str]:
    """Build a ``podrun store`` command from a ParseResult."""
    ns = result.ns
    cmd = [podman_path, 'store']
    action = ns.get('store.action')
    if action:
        cmd.append(action)
        store_dir = ns.get('store.store_dir')
        if store_dir:
            cmd.extend(['--store-dir', store_dir])
        if action == 'init':
            registry = ns.get('store.registry')
            if registry:
                cmd.extend(['--registry', registry])
            storage_driver = ns.get('store.storage_driver')
            if storage_driver:
                cmd.extend(['--storage-driver', storage_driver])
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

    # Route
    if ns['subcommand'] == 'run':
        cmd = build_run_command(result, podman_path)
        if ns['root.print_cmd']:
            print(shlex.join(cmd))
            sys.exit(0)
        # Phase 2: os.execvpe(cmd[0], cmd, ...)
    elif ns['subcommand'] == 'store':
        cmd = build_store_command(result, podman_path)
        if ns['root.print_cmd']:
            print(shlex.join(cmd))
            sys.exit(0)
        # Phase 2: execute store operation
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
