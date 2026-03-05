# Testing

> Back to [README](../README.md) for install and quickstart.

Install dev dependencies first (see [Installing from Source](../README.md#from-source)). All commands
from the root of the repo.

Tests are organized with pytest markers for filtering:

| Marker | Description |
|--------|-------------|
| `live` | Live container integration tests (require podman) |
| `devcontainer` | Devcontainer CLI integration tests (require podman + devcontainer) |

Use `-m` to select or exclude markers.

The `-n` flag controls both parallelism and test scope:

| Flag | Images | Lint/devcontainer | Purpose |
|------|--------|-------------------|---------|
| `-n0` (default) | all 3 | included | full serial suite |
| `-n1` | alpine | excluded | quick functional smoke |
| `-n2` | alpine + ubuntu | excluded | moderate parallel coverage |
| `-n3` | alpine + ubuntu + fedora | excluded | full parallel coverage |

```bash
python3 -m pytest tests/ -n0 -v     # full suite (serial, all images + lint)
python3 -m pytest tests/ -n1 -v     # smoke (alpine only, no lint)
python3 -m pytest tests/ -n3 -v     # full parallel (all images, no lint)
```

Use `-m` to select or exclude markers:
```bash
python3 -m pytest tests/ -m "not live and not devcontainer" -v  # unit tests only
python3 -m pytest tests/ -m live -v                              # live tests only
```

Live and devcontainer tests automatically manage a
[podrun store](store.md) under `.devcontainer/.podrun/store/` so
they do not interfere with your system podman. Use `--registry` with any test
command to pull through a registry mirror (e.g. behind a corporate proxy):
```bash
python3 -m pytest tests/ --registry=my-mirror.example.com -v
```

## Parallel Execution

When `-n` > 0, tests run via pytest-xdist with `--dist loadscope` (tests
grouped by class, one class per worker). Each xdist worker gets its own
isolated podman store so containers don't collide. Lint and devcontainer CLI
tests are automatically deselected because they require serial execution.

## Test Images

Live tests exercise three distro images ranked by test value:

1. **alpine** — busybox/ash fallback paths, no bash
2. **ubuntu** — bash, setpriv, dash as `/bin/sh`
3. **fedora** — bash, gawk, capsh (mostly redundant with ubuntu)

The number of images tested scales with `-n`: `-n1` tests alpine only,
`-n2` adds ubuntu, `-n3` (and `-n0`) tests all three.

## Transient Podman Flakes

Rootless podman has known race conditions that cause sporadic test failures
(typically 2-4 per run out of ~600 tests). These are runtime races in podman
itself, not test bugs. Symptoms:

- `slirp4netns log file ... no such file or directory` — race between
  container startup and slirp4netns network log creation
- `getting exit code of container ... from DB: no such exit code` — race
  between conmon writing the exit code to BoltDB and podman reading it

Different tests fail each run and always pass on retry. These flakes are more
frequent with parallel execution (`-n3`) due to shared podman infrastructure
(`/run/user/<uid>/libpod/`, rootless pause process).
