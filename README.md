# podrun

A podman run superset with host identity overlays. Adds overlay groups for
user identity mapping, host context, and interactive containers, plus
devcontainer.json support and container lifecycle management.

## Installing Podrun

### From Source

All commands below assume you are in the root of the checked-out repo.

Install:
```bash
python3 -m pip install .
```

Development (editable install with test/lint dependencies):
```bash
python3 -m pip install -e '.[dev]'
```

Run without installing:
```bash
python3 -m podrun [OPTIONS] IMAGE [COMMAND...]
```

### From GitHub

To install latest version from GitHub:
```bash
python3 -m pip install git+https://github.com/kschwab/podrun@main
```

To install specific version from GitHub:
```bash
python3 -m pip install git+https://github.com/kschwab/podrun@<VERSION>
```

### Script Only

To install latest version of script:
```bash
wget -nv https://raw.githubusercontent.com/kschwab/podrun/main/podrun/podrun.py -O podrun && chmod a+x podrun
```

To install specific version of script:
```bash
wget -nv https://raw.githubusercontent.com/kschwab/podrun/<VERSION>/podrun/podrun.py -O podrun && chmod a+x podrun
```

## Uninstalling Podrun

```bash
python3 -m pip uninstall podrun -y
```

## Usage

```
podrun [PODRUN_OPTIONS] [PODMAN_OPTIONS] IMAGE [COMMAND...]
podrun [PODRUN_OPTIONS] [PODMAN_OPTIONS] -- [COMMAND...]
```

Podrun accepts all `podman run` flags alongside its own. Any unrecognized flags
are passed through to podman directly. Use `podrun --help` to see both podrun
and podman options together.

### Overlays

Overlays are groups of podman flags that configure common patterns. They can be
combined and each implies its prerequisites:

| Flag | Description |
|---|---|
| `--user-overlay` | Map host user identity into container (userns keep-id, home directory, passwd/group entries, sudo, shell detection) |
| `--host-overlay` | Overlay host system context (implies `--user-overlay`; adds host network, hostname, workspace mount, init, seccomp=unconfined) |
| `--interactive-overlay` | Interactive overlay (`-it`, `--detach-keys=ctrl-q,ctrl-q`) |
| `--workspace` | Workspace overlay (implies `--host-overlay` + `--interactive-overlay`) |
| `--adhoc` | Ad-hoc overlay (implies `--workspace` + `--rm`) |

Use `--print-overlays` to see exactly what each overlay group expands to.

### Exports (Reverse Volumes)

Normal `-v host:container` bind mounts mask the container's content with the
host directory. The `--export` flag goes the other direction: it copies
container-internal files to the host and symlinks the original path to the
host-mounted staging area.

```bash
podrun --user-overlay --export /opt/sdk/bin:./local-sdk my-image
```

**Syntax**: `--export container_path:host_path[:0]`

The mechanism:
1. podrun creates the host directory and bind-mounts it into the container at
   `/.podrun/exports/<hash>`
2. The entrypoint copies the container's original content into the staging area
   (skipped if the host directory is already non-empty)
3. The original container path is replaced with a symlink to the staging area

Both files and directories are supported. Non-existent container paths get a
symlink to the staging directory so that later writes are captured on the host.
Copy-only mode (`:0`) still skips non-existent paths since there is nothing to
copy. Exports require `--user-overlay` (or an overlay that implies it).

**Copy-only mode** (`--export src:dst:0`): Appending `:0` skips the
rm/symlink step. Content is copied to the host but the original container path
is left intact. Use this for paths that contain bind-mounted files (e.g.
`/etc`) where the rm would fail. Other podman volume options (`:ro`, etc.) are
not supported on exports.

**Config equivalent** in `customizations.podrun`:
```json
{
  "customizations": {
    "podrun": {
      "exports": ["/opt/sdk/bin:./local-sdk"]
    }
  }
}
```

### Examples

Ad-hoc container (auto-removes on exit):

```bash
podrun --adhoc ubuntu:24.04
```

Persistent workspace (image survives exit):

```bash
podrun --workspace ubuntu:24.04
```

Non-interactive command execution:

```bash
podrun --host-overlay ubuntu:24.04 -- make -j8
```

Use zsh as the default shell:

```bash
podrun --adhoc --shell zsh ubuntu:24.04
```

Run with a login shell (sources `/etc/profile`):

```bash
podrun --adhoc --login ubuntu:24.04
```

Dry run (print the podman command without executing):

```bash
podrun --adhoc --print-cmd ubuntu:24.04
```

Named container with auto-attach:

```bash
podrun --workspace --name mydev --auto-attach ubuntu:24.04
```

Pass extra podman flags through:

```bash
podrun --adhoc --gpus all -v /data:/data:ro ubuntu:24.04
```

Export container directories to the host:

```bash
# Export container's /opt/sdk/bin to ./local-sdk on the host
podrun --user-overlay --export /opt/sdk/bin:./local-sdk ubuntu:24.04

# Multiple exports
podrun --user-overlay --export /opt/sdk/bin:./sdk --export /usr/share/data:./data ubuntu:24.04
```

### devcontainer.json

Podrun discovers and reads `.devcontainer/devcontainer.json` from the current
directory (searching upward). Supported fields:

```jsonc
{
    "image": "ubuntu:24.04",
    "workspaceFolder": "/workspace",
    "containerEnv": {
        "MY_VAR": "value"
    },
    "remoteEnv": {
        "EDITOR": "vim"
    },
    "mounts": [
        "type=bind,source=/host/data,target=/data",
        { "type": "volume", "source": "cache-vol", "target": "/cache" }
    ],
    "runArgs": ["--device-cgroup-rule=..."],
    "capAdd": ["SYS_PTRACE"],
    "securityOpt": ["seccomp=unconfined"],
    "privileged": false,
    "init": true,
    "customizations": {
        "podrun": {
            "name": "mydev",
            "podmanPath": "/opt/podman/bin/podman",
            "userOverlay": true,
            "hostOverlay": true,
            "interactiveOverlay": true,
            "workspace": true,
            "adhoc": true,
            "shell": "zsh",
            "login": false,
            "x11": false,
            "dood": false,
            "promptBanner": "my-project",
            "autoAttach": true,
            "autoReplace": false,
            "exports": ["/opt/sdk/bin:./local-sdk"],
            "fuseOverlayfs": false,
            "configScript": "/path/to/config.sh",
            "podmanArgs": [
                "--memory=4g",
                "--cpus=2",
                "-v=/data:/data:ro"
            ]
        }
    }
}
```

Top-level fields (`mounts`, `runArgs`, `capAdd`, `securityOpt`, `privileged`,
`init`) are converted to podman flags at the lowest precedence level.
`customizations.podrun.podmanArgs` overrides them, and CLI flags override both.

CLI flags take precedence over `customizations.podrun`, which takes precedence
over top-level devcontainer.json fields.

`podmanPath` specifies the podman binary for podrun to use. It accepts absolute
paths (`/opt/podman/bin/podman`) or bare names resolved from `PATH` (`podman`,
`podman-remote`). If the specified path cannot be found, podrun exits with an
error. When omitted, podrun uses the default `podman` from `PATH`.

Skip devcontainer.json discovery with `--no-devconfig`. Specify an explicit
path with `--config PATH`.

### Config Scripts

The `--config-script` flag runs a script and splices its stdout into the
argument list at the position where the flag appeared:

```bash
podrun --host-overlay --config-script ./my-config.sh ubuntu:24.04
```

Where `my-config.sh` might output:

```
--host-overlay -e HTTP_PROXY=http://proxy.example.com:80
```

Ordering matters: podman uses last-wins semantics, so args after
`--config-script` override the script output, and args before are overridden
by it. Multiple `--config-script` flags are expanded left to right.

Config scripts can also be specified in devcontainer.json via the `configScript`
key in `customizations.podrun`. When specified there, the script output is
prepended to `podmanArgs` (lowest priority). If `--config-script` is used on
the CLI, the devcontainer.json `configScript` is skipped.

### Devcontainer CLI

Podrun can be used as a Docker replacement for the
[devcontainer CLI](https://github.com/devcontainers/cli) via `--docker-path`:

```bash
devcontainer up --docker-path podrun --workspace-folder .
devcontainer exec --docker-path podrun --workspace-folder . echo hello
```

This works because podrun transparently proxies all podman subcommands
(`ps`, `inspect`, `pull`, `build`, `exec`, `version`, `events`, etc.) that
the devcontainer CLI sends. The `run` command is enhanced with podrun's overlay
support based on `customizations.podrun` in your devcontainer.json.

If podrun is not installed as a console script, point `--docker-path` at a
wrapper:

```bash
#!/bin/bash
exec python3 -m podrun "$@"
```

### Container Lifecycle

When a `--name` is provided (or derived from the image), podrun checks for
existing containers:

- **Running container**: prompts to attach or replace (or use `--auto-attach`
  / `--auto-replace` to skip the prompt)
- **Stopped container**: prompts to replace (or use `--auto-replace`)

### Subcommand Passthrough

Podrun transparently proxies podman subcommands it doesn't enhance. Commands
like `ps`, `inspect`, `pull`, `build`, `version`, `exec`, `events`, `stop`,
and `rm` are forwarded directly to podman. Only `run` (and implicit run when
no subcommand is given) receives overlay processing.

```bash
podrun ps -a                    # → podman ps -a
podrun inspect mycontainer      # → podman inspect mycontainer
podrun version --format json    # → podman version --format json
```

This makes podrun a drop-in replacement for podman in tools that expect a
Docker/Podman-compatible CLI (e.g. the devcontainer CLI).

### Podman Flag Compatibility

Podrun maintains a static set of podman value flags to correctly parse
mixed argument lists. Use `--check-flags` to compare the static set against
your installed podman version and identify any flags that need updating.

### Fuse-Overlayfs

Rootless podman uses `--userns=keep-id` to map the host user identity into
the container. On kernels that support native overlay idmap
(`CONFIG_OVERLAY_FS_IDMAP`, added in kernel 5.19), this is instant. On older
or custom kernels that lack this feature, podman falls back to creating an
ID-mapped copy of every image layer — which can hang for minutes on large
images.

The `--fuse-overlayfs` flag tells podrun to use
[fuse-overlayfs](https://github.com/containers/fuse-overlayfs) as the overlay
mount program. Fuse-overlayfs handles UID remapping at the FUSE level,
bypassing the kernel limitation entirely.

**When to use this flag:**

- Container creation hangs or is extremely slow with `--user-overlay` (or any
  overlay that implies it) on large images
- Your kernel is older than 5.19 or lacks `CONFIG_OVERLAY_FS_IDMAP`
- `fuse-overlayfs` is installed on the host (`/usr/bin/fuse-overlayfs`)

**Performance implications:**

- **Container filesystem I/O** (reads/writes within the image layers):
  ~0-5% overhead compared to native overlay. Negligible for most workloads.
- **Bind mount I/O** (host-mounted volumes via `-v`): **zero overhead**.
  Bind mounts go directly through the kernel VFS and bypass FUSE entirely.
  Simulation workloads that operate on mounted host directories see identical
  performance with or without fuse-overlayfs.
- **Overlay volume mounts (`:O`):** fuse-overlayfs can only overlay
  directories, not individual files. When `--fuse-overlayfs` is enabled,
  podrun automatically converts `:O` to `:ro` for single-file volume mounts
  (e.g. `-v=~/.gitconfig:/home/user/.gitconfig:O` becomes
  `-v=~/.gitconfig:/home/user/.gitconfig:ro`). Directory `:O` mounts are
  unaffected.

### Podman Local Storage (Podrun Store)

`podrun store init` creates a venv-style project-local podrun store with
wrapper scripts that inject `--root`/`--runroot`/`--storage-driver` CLI flags.
This keeps all images, layers, and runtime state local to the project without
affecting your system podman.

```bash
podrun store init
source .podrun-store/activate
```

After activation, `podman`, `podrun`, and `python3` in your `PATH` all resolve
to the wrapper scripts under `.podrun-store/bin/`. The prompt shows
`(podrun-store)` to indicate the active store.

```bash
which podman    # .podrun-store/bin/podman
podman images   # shows images in project-local store
podrun --adhoc ubuntu:24.04  # uses project-local storage
```

To deactivate:
```bash
deactivate_podrun_store
```

Options:
```bash
podrun store init --store-dir .podrun-store  # custom directory (default: .podrun-store)
podrun store init --registry mirror.example.com  # configure registry mirror
podrun store info     # show store paths, activation status, and registry config
podrun store destroy  # remove store and its /tmp runroot
```

The runroot (runtime state) lives under `/tmp/podrun-stores/` to avoid NFS
issues and the 108-byte `sun_path` limit. A symlink at `.podrun-store/runroot`
makes the relationship visible. After a reboot, the activate script recreates
the `/tmp` directory automatically.

### Shell Completion

Podrun provides shell completion that wraps podman's built-in Cobra completion
engine. This gives full podman completion (images, containers, flags) with
podrun-specific flags layered on top.

**Bash** -- add to `~/.bashrc`:
```bash
eval "$(podrun --completion bash)"
```

**Zsh** -- add to `~/.zshrc`:
```bash
eval "$(podrun --completion zsh)"
```

**Fish** -- add to `~/.config/fish/config.fish`:
```fish
podrun --completion fish | source
```

After reloading your shell, `podrun <TAB>` will complete subcommands, images,
container names, flags, and all other values that podman's completion supports.
Note that `source .podrun-store/activate` also provides completion since
`podrun` in `bin/` is the real podrun, and shell completion is configured
separately in your shell rc file.

## Testing

Install dev dependencies first (see [From Source](#from-source)). All commands
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
[podrun store](#podman-local-storage-podrun-store) under `.podrun-store/` so
they do not interfere with your system podman. Use `--registry` with any test
command to pull through a registry mirror (e.g. behind a corporate proxy):
```bash
python3 -m pytest tests/ --registry=my-mirror.example.com -v
```

### Parallel Execution

When `-n` > 0, tests run via pytest-xdist with `--dist loadscope` (tests
grouped by class, one class per worker). Each xdist worker gets its own
isolated podman store so containers don't collide. Lint and devcontainer CLI
tests are automatically deselected because they require serial execution.

### Test Images

Live tests exercise three distro images ranked by test value:

1. **alpine** — busybox/ash fallback paths, no bash
2. **ubuntu** — bash, setpriv, dash as `/bin/sh`
3. **fedora** — bash, gawk, capsh (mostly redundant with ubuntu)

The number of images tested scales with `-n`: `-n1` tests alpine only,
`-n2` adds ubuntu, `-n3` (and `-n0`) tests all three.

### Transient Podman Flakes

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

## Run Options

| Flag | Description |
|---|---|
| `--name NAME` | Container name |
| `--user-overlay` | Map host user identity into container |
| `--host-overlay` | Overlay host system context (implies `--user-overlay`) |
| `--interactive-overlay` | Interactive overlay (`-it`, detach keys) |
| `--workspace` | Workspace overlay (implies `--host-overlay` + `--interactive-overlay`) |
| `--adhoc` | Ad-hoc overlay (implies `--workspace` + `--rm`) |
| `--export SRC:DST[:0]` | Export container path to host (requires `--user-overlay`). Append `:0` for copy-only. May be repeated. |
| `--print-overlays` | Print overlay group details and exit |
| `--shell SHELL` | Shell to use inside container (e.g. `bash`, `zsh`) |
| `--login` / `--no-login` | Run shell as login shell (sources `/etc/profile`). `--no-login` explicitly disables. |
| `--x11` | Enable X11 forwarding |
| `--dood` | Docker-outside-of-Docker (Podman socket passthrough) |
| `--prompt-banner TEXT` | Custom prompt banner text |
| `--auto-attach` | Auto attach to named container if already running |
| `--auto-replace` | Auto replace named container if already exists |
| `--print-cmd` / `--dry-run` | Print the podman command instead of executing |
| `--config PATH` | Explicit path to devcontainer.json |
| `--no-devconfig` | Skip devcontainer.json discovery |
| `--config-script PATH` | Run script and inline its stdout as args |
| `--fuse-overlayfs` | Use fuse-overlayfs for overlay mounts (see [Fuse-Overlayfs](#fuse-overlayfs)) |
| `--check-flags` | Diff static podman flags against installed podman |
| `--completion SHELL` | Generate shell completion script (`bash`, `zsh`, `fish`) and exit |
| `--version` | Show version and exit |
| `-h` / `--help` | Show podman run help with podrun options |

## Requirements

- Python >= 3.8
- Podman (rootless)

## License

[MIT](LICENSE.md)
