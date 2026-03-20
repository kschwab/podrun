# Testing

> Back to [README](../README.md) for install and quickstart.

## Quick Start

```bash
python3 -m pip install -e '.[dev]'
python3 -m pytest tests/ -x -q
```

## Architecture

The test suite is unit tests that validate parsing, generation, assembly, and
orchestration logic without running real containers. Tests are parameterized
across available podman binaries (`podman` and `podman-remote`) to verify
flag scraping and command building for both.

## Key Fixtures

All fixtures are defined in `tests/conftest.py`:

| Fixture | Scope | Description |
|---------|-------|-------------|
| `_isolate` | function (autouse) | Universal test isolation: clears env vars (`PODRUN_PODMAN_REMOTE`, `PODRUN_CONTAINER`, `PODRUN_PODMAN_PATH`, `CONTAINER_HOST`), mocks `find_devcontainer_json` and `_default_store_dir` to return None, redirects `PODRUN_TMP` to `tmp_path` |
| `podman_binary` | function (parameterized) | Runs the test once per available binary (`podman`, `podman-remote`); skips unavailable binaries; monkeypatches `_default_podman_path` |
| `podman_only` | function | Restricts a test to the full `podman` binary (deselects `podman-remote` parameterizations) |
| `requires_podman_remote` | function | Restricts a test to `podman-remote` (deselects `podman` parameterizations) |
| `mock_run_os_cmd` | function | Monkeypatches `run_os_cmd` with a `Controller` supporting `set_return()` and `set_side_effect()` |

## Test Files

| File | Tests |
|------|-------|
| `test_podrun_cli.py` | CLI flag parsing, equals-form flags, passthrough |
| `test_podrun_utils.py` | Constants, `_parse_export`, `_parse_image_ref`, tilde expansion, passthrough introspection |
| `test_podrun_config.py` | Config merge, devcontainer parsing, `resolve_config` |
| `test_podrun_entrypoint.py` | `generate_run_entrypoint`, `generate_rc_sh`, `generate_exec_entrypoint` |
| `test_podrun_overlays.py` | Overlay arg builders, dotfiles, caps, validate |
| `test_podrun_state.py` | Container state detection, exec args, overlay command assembly |
| `test_podrun_main.py` | `_handle_run` orchestration, `main()`, nested podrun, podrunrc |
| `test_podrun_store_service.py` | Store service lifecycle, socket/PID management |
| `test_podrun_completions.py` | Bash/zsh/fish completion generators |
| `test_podrun_lint.py` | Ruff, mypy, shellcheck, vulture enforcement |

## Binary State Testing

The test suite is validated against all four podman binary installation states:

| State | How | Expected |
|-------|-----|----------|
| Both binaries | Default (both installed) | All tests pass, 0 skipped, coverage gate enforced |
| podman only | Hide `podman-remote` | `[podman-remote]` params skipped, coverage gate relaxed |
| podman-remote only | Hide `podman` | `[podman]` params skipped, coverage gate relaxed |
| Neither | Hide both | All tests skipped, coverage gate relaxed |

The 95% coverage gate (enforced via `pytest-cov`) is automatically relaxed
when any tests are skipped (incomplete binary set).

## Writing New Tests

1. **Do not** create per-file `_isolate` fixtures — `conftest.py` handles
   isolation via the autouse `_isolate` fixture.
2. Add `pytestmark = pytest.mark.usefixtures('podman_binary')` at module level
   if the test file exercises code that depends on the resolved podman binary
   or scraped flags.
3. Use `@pytest.mark.usefixtures('podman_only')` on tests/classes that use
   flags only available in full podman (e.g. `--root`, `--storage-driver`).
4. For tests needing a `run_os_cmd` mock, request `mock_run_os_cmd` as a
   fixture parameter — do not redefine it in test files.
5. `PODRUN_TMP` is already redirected to `tmp_path` — no need for additional
   temp directory fixtures.

## Lint Enforcement

The `test_podrun_lint.py` file enforces code quality:

| Tool | Checks |
|------|--------|
| **ruff** | `ruff check` + `ruff format --check` on source and tests |
| **mypy** | Type checking on `podrun/podrun.py` |
| **shellcheck** | Entrypoint scripts at `--severity=warning`; completions at `--severity=error` |
| **vulture** | Dead code detection on `podrun/podrun.py` |

---

See also: [Reference](reference.md) for the full flag table.
