---
name: test
description: Use this skill when the user asks to run tests, verify changes, run the test suite, or mentions pytest/testing for the podrun project.
---

Run tests for the podrun project.

## Test infrastructure

- **Framework**: pytest with pytest-xdist (parallel), pytest-cov (coverage)
- **Test directory**: `tests/`
- **Config**: `pyproject.toml` `[tool.pytest.ini_options]`
- **Coverage target**: 95% of `podrun/podrun.py` (enforced when full suite runs with no skips)

## Common commands

```bash
# Unit tests (fast, no live containers)
/usr/bin/python3 -m pytest tests/ -v

# Single test file
/usr/bin/python3 -m pytest tests/test_podrun_main.py -v

# Run with stop-on-first-failure
/usr/bin/python3 -m pytest tests/ -x -q
```

## Binary state testing

**Only applicable inside a podrun container** (`PODRUN_CONTAINER=1`). Do not
attempt binary renaming on the host — it requires sudo and risks breaking the
host's podman installation.

The test suite is validated against all four podman binary installation states.
To cycle through them, temporarily rename binaries with `sudo mv` and run
`/usr/bin/python3 -m pytest tests/ -x -q`:

| State | How | Expected |
|---|---|---|
| Both binaries | Default (both installed) | All tests pass, 0 skipped, coverage gate enforced |
| podman only | `sudo mv /usr/bin/podman-remote /usr/bin/podman-remote.bak` | `[podman-remote]` params skipped, coverage gate relaxed |
| podman-remote only | Hide podman, restore podman-remote | `[podman]` params skipped, coverage gate relaxed |
| Neither | Hide both | All tests skipped, coverage gate relaxed |

**Restore after testing:** `sudo mv /usr/bin/podman.bak /usr/bin/podman` (and
similarly for podman-remote).

## Dependencies

Before running tests, ensure all dependencies are installed. Run these proactively at the start of a test session if there's any doubt packages are present.

**Python dev dependencies** (pytest, ruff, mypy, etc.):

```bash
/usr/bin/python3 -m pip install -e '.[dev]'
```

## Instructions

Always use `/usr/bin/python3` as the Python interpreter.

When the user asks to run tests, interpret their request as follows:

- "unit" or "unit tests" or "tests": run `/usr/bin/python3 -m pytest tests/ -v`
- A test file name or path: run that specific file
- Pytest flags (e.g. `-k`, `-x`, `-v`): pass them through directly
- Otherwise: treat as a `-k` expression to filter tests

Always run from the project root. Show the exact command before running it. After the run, summarize: total tests, passed, failed, skipped, and any coverage percentage reported.
