# Podrun Project Notes

## podrun Transition State

### Phase 1 -- Ported / Updated

| Behavior | Status | Notes |
|---|---|---|
| CLI flag parsing | Updated | Replaced hand-rolled `_PodrunParser`/`_detect_subcommand` with argparse + live podman flag scraping (cached). Flags no longer hardcoded. |
| `--help` rendering | Updated | Now scrapes `podman --help` / `podman run --help` live and appends podrun-specific options. Old version used custom formatter. |
| `--version` | Ported | Same behavior |
| `--print-cmd` / `--dry-run` | Ported | Same behavior |
| devcontainer.json discovery | Ported | Same upward-walk logic |
| JSONC stripping | Ported | Same implementation |
| devcontainer.json parsing + field mapping | Ported | `mounts`, `capAdd`, `securityOpt`, `privileged`, `init`, `runArgs` |
| `customizations.podrun` extraction | Ported | Same behavior |
| Three-way config merge (CLI > script > dc) | Updated | Now uses namespace-dict (`root.*`/`run.*` keys) instead of `Config` dataclass. Merge logic is cleaner but equivalent precedence. |
| Config-script execution + token parsing | Updated | New `run_config_scripts()` + `parse_config_tokens()` replace `_expand_config_scripts()` + `_resolve_config_script()`. Scripts run through same root+run parsers. |
| Overlay implication chain (adhoc->session->host+interactive->user) | Ported | In `resolve_config()` |
| Image resolution from dc `image` field | Ported | Falls back to dc image when no CLI trailing args |
| Export merging (dc + script + cli) | Ported | Append order preserved |
| Label-based dc config path (`devcontainer.config_file=`) | Ported | Same behavior |
| `--no-devconfig` | Ported | Same behavior |
| Local store init / destroy / info | Updated | Simplified signatures (take `store_dir: str` instead of `args` namespace). Same fs layout (graphroot + runroot symlink). |
| Store auto-discovery (`_default_store_dir`) | Ported | Same upward-walk logic |
| `--root`/`--runroot`/`--storage-driver` injection | Updated | New `_resolve_store()` + `_apply_store()` handle conflict checks and podman-remote guard. |
| Podman remote detection | New | `is_podman_remote()` -- used to skip store flags on remote clients. Not in podrun.py's store path (was implicit). |
| Podman flag scraping + caching | New | Live scrape of `podman --help` / `podman run --help` with disk cache per version. Replaces hardcoded `PODMAN_RUN_VALUE_FLAGS`. |
| Passthrough subcommands (ps, images, etc.) | Updated | Empty subparsers per scraped subcommand; `build_passthrough_command()` + `os.execvpe()`. |

### Phase 1 -- Deprecated (replaced in Phase 2)

| Behavior | Notes |
|---|---|
| `Config` dataclass | Replaced by flat `ns` dict with `root.*`/`run.*` keys in `ParseResult` |
| `_PodrunParser` / `_PodrunMutuallyExclusiveGroup` / `_PodrunSubParsers` | Replaced by standard argparse + `_PassthroughAction` |
| `_detect_subcommand()` (manual argv walk) | Replaced by argparse subparsers |
| `_ProjectContext` / `_find_project_context()` | Combined store+dc walk replaced by separate `_default_store_dir()` + `find_devcontainer_json()` |
| Hardcoded `PODMAN_RUN_VALUE_FLAGS` / `PODMAN_SUBCOMMANDS` | Replaced by live scraping into `PodmanFlags` |
| `merge_config()` (monolithic) | Replaced by `resolve_config()` with cleaner separation |
| `_expand_volume_tilde()` / `_expand_export_tilde()` | Ported in Phase 2.1. Enhanced for space-separated form in Phase 2.4. Used by `_DOTFILES` tilde expansion in Phase 2.10. |
| `check_flags()` / `_scrape_podman_value_flags()` (diff tool) | No longer needed -- flags are scraped live |

### Phase 2 -- Porting Plan

| Phase | Test file |
|---|---|
| 1.x | `tests/test_podrun_cli.py` |
| 2.1 | `tests/test_podrun_utils.py` |
| 2.2 | `tests/test_podrun_entrypoint.py` |
| 2.3 | `tests/test_podrun_overlays.py` |
| 2.4 | `tests/test_podrun_state.py` |
| 2.5 | `tests/test_podrun_main.py` |
| 2.6 | `tests/test_podrun_store_service.py` |
| 2.7 | `tests/test_podrun_completions.py` |
| 2.8 | `tests/test_podrun_lint.py` |

### CLI flag form coverage

`tests/test_podrun_cli.py` includes `TestEqualsFormRootFlags`,
`TestEqualsFormRunFlags`, and `TestEqualsFormPassthroughFlags` — 44 tests
ensuring every value flag parses correctly in both `--flag=value` and
`--flag value` forms. Coverage includes:

- **Root/global:** `--devconfig=`, `--config-script=`, `--completion=`,
  `--log-level=`, `--storage-opt=`
- **Run (podrun):** `--name=`, `--shell=`, `--prompt-banner=`, `--export=`,
  `--label=`, `-l=`
- **Passthrough (podman run):** `-e=`/`--env`, `-v=`/`--volume=`,
  `-m=`/`--memory`/`--memory=`, `-u=`/`--user`/`--user=`,
  `-w`/`-w=`/`--workdir`/`--workdir=`, `-p=`/`--publish`/`--publish=`,
  `-h=`/`--hostname`/`--hostname=`, `--network=`, `--mount=`, `--cpus=`,
  `--cap-add`/`--cap-add=`, `--entrypoint`/`--entrypoint=`,
  `--userns`/`--userns=`, `--annotation=`,
  `--security-opt`/`--security-opt=`

**Guiding principle for every sub-phase:** look for opportunities to simplify
the ported code by leveraging the `ns` dict, `ParseResult`, argparse
backbone, and existing helpers (`build_run_command`, `resolve_config`,
`_apply_store`). Specifically:

- **`ns` dict replaces `Config` dataclass** -- functions should read
  `ns.get('run.field')` directly instead of accepting a `Config` object.
  No intermediate dataclass to build or maintain.
- **Overlay args inject into `ns['run.passthrough_args']`** before calling
  the existing `build_run_command()`, rather than rebuilding the full command.
- **`resolve_config()` and `_apply_store()` already run in `main()`**, so the
  Phase 2 run handler is purely: state -> entrypoints -> overlays -> exec.
- **Argparse already collects passthrough** via `_PassthroughAction`, so
  manual flag accumulation code can be dropped.
- **`PodmanFlags` live-scrape** replaces hardcoded flag sets -- validation
  can reference scraped data instead of static frozensets where appropriate.

#### Phase 2.1: Constants, Utilities, and Parsing Helpers ✓

Foundation layer. All pure functions, no side effects, immediately testable.

| Item | Source (podrun.py) | Notes |
|---|---|---|
| Module constants | `UID`, `GID`, `UNAME`, `USER_HOME`, `PODRUN_TMP`, `PODRUN_*_PATH`, `BOOTSTRAP_CAPS`, `_OVERLAY_FIELDS` | Top-of-module, used everywhere downstream |
| `_parse_export()` | Lines 295-308 | Export spec parsing (`SRC:DST[:0]`) |
| `_parse_image_ref()` | Lines 2699-2723 | Image ref splitting for `PODRUN_IMG*` env vars |
| Passthrough introspection | `_passthrough_has_flag`, `_passthrough_has_exact`, `_passthrough_has_short_flag` (lines 2725-2743) | Pure string checks on arg lists |
| Passthrough extraction | `_extract_passthrough_entrypoint`, `_volume_mount_destinations` (lines 2744-2809) | Extract/remove flags from passthrough |
| Tilde expansion | `_expand_volume_tilde`, `_expand_export_tilde` (lines 1953-1996) | `~/` -> `$HOME/` in volumes and exports |
| `_write_sha_file()` | Lines 2298-2313 | Idempotent SHA-named script writer under `PODRUN_TMP` |
| `yes_no_prompt()` | Lines 320-336 | Interactive Y/N prompting for lifecycle decisions |

#### Phase 2.2: Entrypoint Generation ✓

Self-contained shell script generators (~330 lines, mostly templates).
Take `ns` dict directly instead of `Config` dataclass.

| Item | Source (podrun.py) | Notes |
|---|---|---|
| `generate_run_entrypoint()` | Lines 2316-2489 | UID/GID/passwd, home dir, shell, sudo, caps, exports |
| `generate_rc_sh()` | Lines 2497-2582 | Prompt banner, CPU/vCPU info, stty |
| `generate_exec_entrypoint()` | Lines 2583-2654 | READY sentinel wait, shell resolution, login flag |

Depends on 2.1 (`_write_sha_file`, `_parse_export`).

#### Phase 2.3: Overlay Arg Builders ✓

Each builder returns a list of podman args. Read from `ns` dict directly.
**Status: Complete — 73 tests in `tests/test_podrun_overlays.py`.**

| Item | Source (podrun.py) | Notes |
|---|---|---|
| `compute_caps_to_drop()` | New | Filters `BOOTSTRAP_CAPS` vs user `--cap-add`/`--privileged` |
| `_user_overlay_args()` | Lines 2810-2830 | Returns `(args, caps_to_drop)` tuple; `--userns=keep-id`, passwd-entry, caps, entrypoint mounts, export volumes |
| `_host_overlay_args()` | Lines 2842-2860 | hostname, network, seccomp, workdir mount, localtime |
| `_interactive_overlay_args()` | Lines 2833-2839 | `-it`, detach-keys, `--init` |
| `_dot_files_overlay_args()` | New | Mount-mode dotfiles (`.emacs`, `.emacs.d`, `.vimrc`) from host HOME into container |
| `_x11_args()` | Lines 2863-2874 | X11 socket + DISPLAY |
| `_podman_remote_args()` | Lines 2877-2893 | Socket passthrough, CONTAINER_HOST |
| `_env_args()` | Lines 2896-2918 | PODRUN_* env vars |
| `_validate_overlay_args()` | Lines 2921-2954 | Conflict checks |
| `print_overlays()` | Lines 2662-2697 | `--print-overlays` implementation |

Key decisions:
- `generate_run_entrypoint()` gained a `caps_to_drop` parameter (default: all BOOTSTRAP_CAPS)
- `_user_overlay_args()` returns `(args, caps_to_drop)` so orchestration can pass filtered caps to entrypoint generation
- `compute_caps_to_drop(pt)` handles `--cap-add` (equals/space/comma forms, case-insensitive) and `--privileged`
- `--dot-files-overlay`/`--dotfiles` CLI flag added; implies `user_overlay` via `resolve_config()`
- `_DOTFILES` unified list replaces `_DOTFILES_MOUNT` — entries use `-v=` syntax with `:ro` (mount-mode) or `:0` (copy-mode). Copy-mode items (`.ssh`, `.gitconfig`) resolved by `_resolve_overlay_mounts` via entrypoint copy-staging (Phase 2.10)

#### Phase 2.4: Command Assembly + Container State ✓

Wire overlay args into the existing command-building backbone.
**Status: Complete — 65 tests in `tests/test_podrun_state.py`.**

| Item | Source (podrun.py) | Notes |
|---|---|---|
| `detect_container_state()` | Lines 2224-2242 | `podman inspect` state query; returns "running"/"stopped"/None |
| `handle_container_state()` | Lines 2245-2290 | Action decision: run/attach/replace/None; reads `run.name`, `run.auto_attach`, `run.auto_replace` |
| `query_container_info()` | Lines 3021-3043 | Inspect running container env for PODRUN_WORKDIR/PODRUN_OVERLAYS |
| `build_podman_exec_args()` | Lines 3044-3089 | Exec command for attach sessions; passes shell/login overrides as env vars |
| `build_overlay_run_command()` | Lines 2957-3020 | Generates entrypoints, calls overlay builders, injects into passthrough, delegates to `build_run_command()` |

Key decisions:
- `build_overlay_run_command(result)` returns `(cmd, caps_to_drop)` tuple
- Alt-entrypoint extraction: when user-overlay active, `--entrypoint` from passthrough is extracted and passed as `PODRUN_ALT_ENTRYPOINT` env
- `_expand_volume_tilde()` enhanced to handle space-separated form (`-v ~/src`) from `_PassthroughAction`, not just equals form (`-v=~/src`)
- `build_podman_exec_args()` takes ns dict + name + container_workdir + trailing_args + explicit_command; command from explicit_command takes priority over trailing_args

#### Phase 2.5: Main Orchestration + Execution ✓

Final integration into `main()`. Tests: `tests/test_podrun_main.py` (40 tests).

| Item | Source (podrun.py) | Status |
|---|---|---|
| `_is_nested()` | replaces `is_podman_remote()` | ✓ Single source of truth for nested-execution detection via `PODRUN_CONTAINER` env var |
| `_default_podman_path()` | Lines 237-245 | ✓ `PODRUN_PODMAN_PATH` env var → nested podman-remote → podman fallback |
| `_warn_missing_subids()` | Lines 1416-1439 | ✓ subuid/subgid check |
| `_fuse_overlayfs_fixup()` | Lines 3193-3218 | ✓ Replaced by `_resolve_overlay_mounts()` in Phase 2.10 — `:O`→copy-staging fallback + storage-opt injection |
| `_handle_run()` | Lines 3103-3226 | ✓ state → entrypoints → overlays → exec |
| `main()` updated | — | ✓ Nested guard via `_is_nested()`, `_default_podman_path()`, routes to `_handle_run()` |
| `_volume_mount_destinations()` | — | ✓ Fixed space-form volume parsing (`-v /host:/ctr`) |

Key decisions:
- **`PODRUN_PODMAN_PATH`** env var support in `_default_podman_path()` — highest-priority override for the podman binary path, checked before any parsing or flag scraping. Follows the standard `CC`/`EDITOR` convention. Resolved via `shutil.which()` (handles bare names and absolute paths); exits with error if not found. Avoids chicken-and-egg problem of CLI/devcontainer `podmanPath` (binary needed before parsing, but config not available until after).
- **`PODRUN_CONTAINER=1`** is set by `_env_args()` in every child container. It is the single source of truth for "am I inside a podrun container?" — used by `_is_nested()`, which replaced the old `is_podman_remote()` function (which spawned `podman info`). All guards (nested-run refusal, podman-remote preference, store-flag suppression, flag-scrape refusal) go through `_is_nested()`.
- `_handle_run()` orchestrates: image extraction → container state → export conflict filtering → subid warning → overlay build → fuse-overlayfs fixup → stale cleanup → exec
- `_volume_mount_destinations()` handles both equals form (`-v=/host:/ctr`) and space form (`-v /host:/ctr`) from `_PassthroughAction`
- `TestPrintCmdOutput` tests updated to use structural assertions (not exact equality) since `_handle_run` injects PODRUN_* env vars
Depends on 2.1-2.4.

#### Phase 2.6: Store Service Lifecycle ✓

Store service lifecycle for `podman system service` management.
**Status: Complete — 35 tests in `tests/test_podrun_store_service.py`.**

| Item | Source (podrun.py) | Notes |
|---|---|---|
| `_store_hash()` | New | Extracted from `_runroot_path`; shared by socket/pid/runroot path helpers |
| `_store_socket_path()` | Line 1308 | Socket path from graphroot |
| `_store_pid_path()` | Line 1314 | PID file path |
| `_socket_is_alive()` | Lines 1320-1330 | Health check (PID alive + socket exists) |
| `_wait_for_socket()` | Lines 1332-1342 | Block until ready, warns on timeout |
| `_ensure_store_service()` | Lines 1344-1394 | Idempotent start of `podman system service`; writes PID, waits for socket |
| `_stop_store_service()` | Lines 1395-1415 | SIGTERM → clean PID file → clean socket (was empty stub) |
| `_is_nested()` hardened | — | Fallback: `CONTAINER_HOST` + `PODRUN_SOCKET_PATH` existence |
| `PODRUN_SOCKET_PATH` | New | `/.podrun/podman/podman.sock` — podrun-specific mount point |
| `PODRUN_CONTAINER_HOST` | New | `unix://` + `PODRUN_SOCKET_PATH` |

Key decisions:
- **Socket mount moved to `/.podrun/podman/podman.sock`** — replaces `/run/podman/podman.sock`. This path only exists inside a podrun container, making it an unambiguous signal for `_is_nested()` fallback detection
- **`_is_nested()` hardened**: primary check via `PODRUN_CONTAINER` env var (fast path); fallback checks `CONTAINER_HOST == PODRUN_CONTAINER_HOST` AND socket file exists at `PODRUN_SOCKET_PATH` (tamper-resistant — survives `unset PODRUN_CONTAINER`)
- **`_store_hash()` extracted** from `_runroot_path()` to eliminate triple `hashlib.sha256` duplication across `_runroot_path`, `_store_socket_path`, `_store_pid_path`
- **`_handle_run()` integration**: when `run.podman_remote` and `root.local_store` are both set, calls `_ensure_store_service()` and sets `ns['run.store_socket']` before overlay command assembly

#### Phase 2.7: Shell Completion ✓

Bash/zsh/fish completion script generators.
**Status: Complete — 40 tests in `tests/test_podrun_completions.py`.**

| Item | Source (podrun.py) | Notes |
|---|---|---|
| `_completion_data()` | New | Introspects argparse parsers to build flag metadata; auto-picks up new podrun flags |
| `_generate_bash_completion()` | Lines 818-972 | Simplified: no nested subcommand handling |
| `_generate_zsh_completion()` | Lines 974-1136 | Simplified: no nested subcommand handling |
| `_generate_fish_completion()` | Lines 1137-1297 | Simplified: no nested subcommand handling |

Key decisions:
- **`_completion_data()` introspects parsers** — iterates `parser._actions` on root and run parsers, collecting option strings where `dest` starts with `root.` or `run.`. Classifies as value flag based on action type. Automatically picks up new podrun flags without hardcoded lists.
- **No subcmd context blocks** — the `store` subcommand was replaced with `--local-store-*` global flags, eliminating nested subcommand completion. All three generators are simplified by removing `podrun_subcommands`, `sub_flag_cases`, and `sub_flag_case_block`.
- **Same Cobra delegation pattern** — strip podrun flags from command line, inject implicit `run`, delegate to `podman __completeNoDesc` (bash) / `podman __complete` (zsh/fish), merge podrun flags when current word starts with `-`.

#### Phase 2.8: Linting + Coverage ✓

Ruff, mypy, shellcheck, vulture, and pytest-cov enforcement.
**Status: Complete — 9 tests in `tests/test_podrun_lint.py`.**

| Item | Notes |
|---|---|
| `TestRuff` (2 tests) | `ruff check` + `ruff format --check` on `podrun/podrun.py` and `tests/` |
| `TestMypy` (1 test) | `mypy podrun/podrun.py` — type annotations added for all errors |
| `TestShellcheck` (5 tests) | run-entrypoint, rc.sh, exec-entrypoint at `--severity=warning`; bash/zsh completions |
| `TestVulture` (1 test) | Dead code detection on `podrun/podrun.py` |
| Coverage threshold | Enforced via `--cov-fail-under=95` in `pyproject.toml` addopts (no dedicated test) |

Key decisions:
- **Ruff fixes**: 26 auto-fixed (F401 unused imports, F541 extraneous f-prefixes), 8 manual (C901 `# noqa: C901` on 4 orchestration functions, E741 `l`→`ln` rename, F841 dead code removal)
- **Mypy fixes**: `Optional` for defaulting-to-None params, `# type: ignore[attr-defined]` for private argparse attributes (`_run_subparser`, `_subparsers._group_actions`), `# type: ignore[union-attr]` for `_subparsers` access, type annotations on untyped variables
- **Shellcheck**: `--severity=warning` for entrypoint scripts (fixed `uid=$(id -u)` → `uid="$(id -u)"` SC2046); `--severity=error` for zsh completion (zsh-specific constructs trigger false positive warnings in bash mode); fish completion skipped (shellcheck doesn't support fish)
- **Vulture**: dead code detection on `podrun/podrun.py`; `podrun_whitelist.py` removed (no longer needed after dead code cleanup)
- **Coverage**: `--cov-fail-under=95` in `pyproject.toml` addopts; threshold at 95% (current ~96%)

#### Phase 2.9: Rename podrun2 → podrun ✓

Renamed `podrun/podrun2.py` → `podrun/podrun.py`, merged `tests2/` → `tests/`.
**Status: Complete.**

| Item | Notes |
|---|---|
| `podrun/podrun2.py` → `podrun/podrun.py` | Module renamed; old podrun1 `podrun/podrun.py` deleted |
| `tests2/*.py` → `tests/test_podrun_*.py` | All 10 test files + conftest moved and renamed |
| All imports updated | `podrun.podrun2` → `podrun.podrun`, `podrun2_mod` → `podrun_mod` |
| `test_podrun_lint.py` paths updated | `_TARGETS`, mypy, vulture, coverage all point to new paths |
| `live_tests_reference/test_live.py` | Old live tests preserved for reference; pending Phase 3 rewrite |

Key decisions:
- Old `podrun/podrun.py` (podrun1) deleted — no backward-compat shim
- `tests/` directory fully replaced — old podrun1 tests removed
- `__init__.py`, `__main__.py`, `pyproject.toml` required no changes
  (`from .podrun import main` already points to the renamed module)
- Live integration tests preserved in `live_tests_reference/` pending Phase 3

#### Phase 2.10: Copy-mode Dotfiles + `:O` Entrypoint-Copy Fallback ✓

Copy-mode dotfiles and unified `:O`/`:0` overlay mount resolution.
**Status: Complete — tests in `tests/test_podrun_overlays.py`, `tests/test_podrun_entrypoint.py`, `tests/test_podrun_main.py`.**

| Item | Notes |
|---|---|
| `_DOTFILES` | Unified list replaces `_DOTFILES_MOUNT`. Entries use `-v=` syntax: `:ro` for mount-mode (`.emacs`, `.emacs.d`, `.vimrc`), `:0` for copy-mode (`.ssh`, `.gitconfig`) |
| `_dot_files_overlay_args()` | Returns raw `-v=` args from `_DOTFILES` whose host paths exist. Tilde expansion and `:0` resolution happen downstream |
| `_copy_staging_args(items)` | New. Builds staging dirs under `PODRUN_TMP/copy-staging/` + podman mount args. Files: one mount (data copied at build time). Dirs: two mounts (metadata + data bind) |
| `_extract_copy_staging(args)` | New. Extracts `:0` volume entries from arg lists, returns `(filtered_args, items)` |
| `_resolve_overlay_mounts(ctx)` | Replaces `_fuse_overlayfs_fixup(ns)`. Handles `--fuse-overlayfs` storage-opt injection AND `:O`/`:0` mount fallback. No longer gated on `--fuse-overlayfs` flag |
| `generate_run_entrypoint()` | Added generic copy-staging loop after home dir setup (before sudo). Iterates `/.podrun/copy-staging/*`, reads `.podrun_target`, copies `data` to target, chowns |
| `build_overlay_run_command()` | After tilde expansion, extracts `:0` items from overlay_args + passthrough, builds staging mounts |
| `PodrunContext.copy_staging` | New optional field for `:O` fallback items from `_resolve_overlay_mounts` |
| Session implication chain | `session` now implies `dot_files_overlay` (was: session→host+interactive→user) |

Key decisions:
- **Entrypoint copy block** chosen over `:O` overlay because: (a) `:O` doesn't work on individual files like `.gitconfig`, (b) fuse-overlayfs may not be available, (c) single mechanism for all copy-mode items
- **`:0` suffix** is the explicit writable-copy marker in `-v=` args. Distinct from `:O` (overlay) — `:0` always uses entrypoint copy, `:O` uses native overlay when possible
- **`_resolve_overlay_mounts` fallback priority**: `:0` → always copy-staging; `:O` file → copy-staging; `:O` dir + fuse-overlayfs → native; `:O` dir − fuse-overlayfs → copy-staging
- **`--fuse-overlayfs` flag kept** — its meaning is `--storage-opt overlay.mount_program=...` injection for kernels without `CONFIG_OVERLAY_FS_IDMAP`. Orthogonal to the `:O` fallback logic. The flag no longer gates `:O` handling (that's automatic)
- **Self-describing staging entries** — each `/.podrun/copy-staging/{sha12}/` contains `.podrun_target` (destination path) and `data` (content). Entrypoint iterates generically without knowing the dotfile list
- **Session implies dotfiles** — `session` → `host+interactive+dotfiles` → `user`. Previous chain was `session` → `host+interactive` → `user`
- **`_DOTFILES` uses `-v=~/.ssh:~/.ssh:0` syntax** — tilde expanded by `_expand_volume_tilde()` downstream, consistent with passthrough volume handling

#### Phase 2.11: Nested Podrun via Cache-Aware Flag Filtering ✓

Enable nested podrun execution (running podrun inside a podrun container).
**Status: Complete — tests in `tests/test_podrun_main.py` and `tests/test_podrun_cli.py`.**

| Item | Notes |
|---|---|
| `_flags_cache_path()` | Added `podman_path` parameter; uses `os.path.basename()` so `podman` and `podman-remote` get separate cache files |
| `_write_flags_cache()` | Wrapped in `try/except OSError: pass` for read-only cache dirs inside containers |
| `load_podman_flags()` | Removed `_is_nested()` scraping refusal; passes `podman_path` to `_flags_cache_path()` |
| `_filter_global_args()` | New function: filters `ns['podman_global_args']` against loaded `PodmanFlags`, silently dropping unknown flags + values |
| `main()` | Removed blanket `_is_nested()` → `sys.exit(1)` guard; pre-loads flags with resolved `podman_path`; calls `_filter_global_args()` before command building |
| `_handle_run()` | `_warn_missing_subids()` skipped when nested (misleading `/etc/subuid`); `_resolve_overlay_mounts()` skipped when nested (storage on remote daemon) |
| `conftest.py` | Seeds `podman-remote` in-memory cache from host cache files |

Key decisions:
- **`_filter_global_args()` is the single gate for binary flag compatibility** — the scraped flag cache for `podman-remote` has fewer global flags than `podman`. `_filter_global_args()` uses this as source of truth to drop unsupported flags (e.g. `--root`, `--storage-driver`) silently. Callers like `_apply_store`, `_resolve_overlay_mounts`, and config scripts inject flags without caring which binary is in use — filtering is centralized in `main()`.
- **`_apply_store()` nested guard is semantic, not flag-related** — `_resolve_store` is skipped when nested because the store filesystem lives on the host (not about flag compatibility). `--local-store-destroy` still errors when nested. `--local-store-info` prints "disabled".
- **`_is_nested()` detection unchanged** — primary via `PODRUN_CONTAINER` env var, fallback via `CONTAINER_HOST` + socket existence.

### Binary State Testing

The test suite is validated against all four podman binary installation states.
To cycle through them, temporarily rename binaries with `sudo mv` and run
`python3 -m pytest tests/ -x -q`:

| State | How | Expected |
|---|---|---|
| Both binaries | Default (both installed) | All tests pass, 0 skipped, coverage gate enforced |
| podman only | `sudo mv /usr/bin/podman-remote /usr/bin/podman-remote.bak` | `[podman-remote]` params skipped, coverage gate relaxed |
| podman-remote only | Hide podman, restore podman-remote | `[podman]` params skipped, coverage gate relaxed |
| Neither | Hide both | All tests skipped, coverage gate relaxed |

**Restore after testing:** `sudo mv /usr/bin/podman.bak /usr/bin/podman` (and
similarly for podman-remote).

Key infrastructure and fixture guidance:

- **`_isolate`** (conftest.py, autouse) — universal test isolation applied to
  every test automatically: clears `PODRUN_PODMAN_REMOTE`, `PODRUN_CONTAINER`,
  `PODRUN_PODMAN_PATH`, `CONTAINER_HOST` env vars; mocks
  `find_devcontainer_json` and `_default_store_dir` to return None; redirects
  `PODRUN_TMP` to `tmp_path`. **Do not duplicate this in test files.**
- **`podman_binary`** (conftest.py, parameterized) — runs the test once per
  available binary (`podman`, `podman-remote`); skips unavailable binaries;
  monkeypatches `_default_podman_path`. Test files opt in with
  `pytestmark = pytest.mark.usefixtures('podman_binary')` at module level.
- **`podman_only`** / **`requires_podman_remote`** (conftest.py) — restrict a
  test to one binary. Use `@pytest.mark.usefixtures('podman_only')` on a class
  or test function. Incompatible parameterizations are **deselected** (not
  skipped) at collection time via `pytest_collection_modifyitems`.
- **`mock_run_os_cmd`** (conftest.py) — monkeypatches `run_os_cmd` with a
  `Controller` that supports `set_return()` and `set_side_effect()`. Request it
  as a test parameter; do not redefine in test files.
- **Coverage gate** — enforced only on full runs with 0 skipped tests.
  `pytest_terminal_summary` (tryfirst) disables `cov_fail_under` before
  pytest-cov checks it when any tests are skipped.

When writing new tests:

1. **Do not** create per-file `_isolate` fixtures — conftest handles isolation.
2. Add `pytestmark = pytest.mark.usefixtures('podman_binary')` if the test file
   exercises code that depends on the resolved podman binary or scraped flags.
3. Use `@pytest.mark.usefixtures('podman_only')` on tests/classes that use flags
   only available in full podman (e.g. `--root`, `--storage-driver`).
4. For tests needing a `run_os_cmd` mock, use `mock_run_os_cmd` from conftest or
   a class-level fixture that only patches `run_os_cmd` (not PODRUN_TMP).
5. `PODRUN_TMP` is already redirected to `tmp_path` — no need for class-level
   `_tmp_dir` fixtures unless adding extra mocking.

### Phase 3 — Live Testing + Bug Fixes

Live container integration tests and bug fixes discovered during end-to-end
testing. Unit tests cover parsing, generation, and assembly logic; Phase 3
validates the full podrun lifecycle against real podman.
