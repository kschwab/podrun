---
name: test
description: Use this skill when the user asks to run tests, verify changes, run the test suite, or mentions pytest/testing for the podrun project.
---

Run tests for the podrun project.

## Test infrastructure

- **Framework**: pytest with pytest-xdist (parallel), pytest-cov (coverage)
- **Test directory**: `tests/`
- **Config**: `pyproject.toml` `[tool.pytest.ini_options]`
- **Coverage target**: 100% of `podrun/podrun.py` (enforced when full suite runs serial with no skips)

## Test categories

| Marker | Description |
|--------|-------------|
| *(none)* | Unit tests (no podman required) |
| `live` | Live container integration tests (require podman) |
| `devcontainer` | Devcontainer CLI integration tests (require podman + devcontainer) |

## Parallelism (`-n` flag)

The `-n` flag controls both parallelism and test image scope:

| Flag | Images | Lint/devcontainer | Purpose |
|------|--------|-------------------|---------|
| `-n0` | all 3 | included | full serial suite |
| `-n1` | alpine | excluded | quick smoke |
| `-n2` | alpine + ubuntu | excluded | moderate parallel |
| `-n3` | alpine + ubuntu + fedora | excluded | full parallel |

Lint and devcontainer tests (`test_lint.py`, `test_devcontainer_cli.py`) are auto-deselected when `-n` > 0.

## Common commands

```bash
# Unit tests only (fast, no podman needed)
/usr/bin/python3 -m pytest tests/ -m "not live and not devcontainer" -v

# Quick smoke (alpine only, parallel)
/usr/bin/python3 -m pytest tests/ -n1 -v --registry=ext-docker-docker-io-remote.boartifactory.micron.com

# Full serial suite (all images + lint, enforces 100% coverage)
/usr/bin/python3 -m pytest tests/ -n0 -v --registry=ext-docker-docker-io-remote.boartifactory.micron.com

# Full parallel (all 3 images, no lint)
/usr/bin/python3 -m pytest tests/ -n3 -v --registry=ext-docker-docker-io-remote.boartifactory.micron.com

# Live tests only
/usr/bin/python3 -m pytest tests/ -m live -v --registry=ext-docker-docker-io-remote.boartifactory.micron.com

# Single test file
/usr/bin/python3 -m pytest tests/test_podman_args.py -v --registry=ext-docker-docker-io-remote.boartifactory.micron.com

# Single test file with live tests
/usr/bin/python3 -m pytest tests/test_podman_args.py -v --registry=ext-docker-docker-io-remote.boartifactory.micron.com
```

## Dependencies

Before running tests, ensure all dependencies are installed. Run these proactively at the start of a test session if there's any doubt packages are present.

**Python dev dependencies** (pytest, ruff, mypy, etc.):

```bash
/usr/bin/python3 -m pip install -e '.[dev]'
```

**Live test dependencies** — live and smoke tests need `podman-remote` available inside the container:

```bash
# Check if podman-remote is installed
which podman-remote || sudo dnf install -y podman-remote
```

## Instructions

Always use `/usr/bin/python3` as the Python interpreter. Always include `--registry=ext-docker-docker-io-remote.boartifactory.micron.com` when running any tests that involve live containers (smoke, full, parallel, live).

When the user asks to run tests, interpret their request as follows:

- "unit" or "unit tests": run unit tests only (`-m "not live and not devcontainer"`) — no `--registry` needed
- "smoke": run `-n1 -v --registry=ext-docker-docker-io-remote.boartifactory.micron.com`
- "full": run `-n0 -v --registry=ext-docker-docker-io-remote.boartifactory.micron.com` (serial, all images + lint, coverage enforced)
- "parallel": run `-n3 -v --registry=ext-docker-docker-io-remote.boartifactory.micron.com`
- "live": run `-m live -v --registry=ext-docker-docker-io-remote.boartifactory.micron.com`
- A test file name or path: run that specific file (add `--registry` if the file contains live tests)
- Pytest flags (e.g. `-k`, `-m`, `-v`): pass them through directly (add `--registry` if live tests are included)
- Otherwise: treat as a `-k` expression to filter tests

Always run from the project root. Show the exact command before running it. After the run, summarize: total tests, passed, failed, skipped, and any coverage percentage reported.

## Known flakes

Rootless podman has race conditions that cause sporadic failures in live tests (typically 2-4 per run out of ~600 tests). These are podman runtime bugs, not test bugs. Symptoms include `slirp4netns` file-not-found errors and `no such exit code` errors. Different tests fail each run and pass on retry. Do not treat these as real failures unless the same test fails consistently.
