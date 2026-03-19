# Reference

> Back to [README](../README.md) for install and quickstart.

## Global Flags

These flags apply to all subcommands and must appear before the subcommand:

| Flag | Description |
|------|-------------|
| `--print-cmd` / `--dry-run` | Print the podman command instead of executing it |
| `--devconfig PATH` | Explicit path to devcontainer.json |
| `--config-script PATH` | Run script and parse its stdout as flags (may be repeated) |
| `--no-devconfig` | Skip devcontainer.json discovery |
| `--no-podrunrc` | Skip `~/.podrunrc*` discovery |
| `--completion SHELL` | Generate shell completion script (`bash`, `zsh`, `fish`) and exit |
| `--version` / `-v` | Show version and exit |
| `--local-store DIR` | Use project-local store directory (see [Local Store](local-store.md)) |
| `--local-store-ignore` | Suppress auto-discovery of project-local store |
| `--local-store-auto-init` | Auto-create store if missing (uses `--local-store` or auto-discovered path) |
| `--local-store-info` | Print store information and exit |
| `--local-store-destroy` | Remove project-local store before proceeding |

## Run Flags

| Flag | Description |
|------|-------------|
| `--user-overlay` | Map host user identity into container (`--userns=keep-id`, home dir, passwd entry, shell, sudo, bootstrap caps) |
| `--host-overlay` | Host system context (implies `--user-overlay`; adds hostname, `--network=host`, `seccomp=unconfined`, workspace mount, `/etc/localtime`, git submodule auto-resolution) |
| `--interactive-overlay` | Interactive terminal (`-it`, `--init`, `--detach-keys=ctrl-q,ctrl-q`) |
| `--session` | Session overlay (implies `--host-overlay` + `--interactive-overlay` + `--dotfiles`) |
| `--adhoc` | Ad-hoc overlay (implies `--session` + `--rm`) |
| `--dot-files-overlay` / `--dotfiles` | Mount host dotfiles into container (implies `--user-overlay`) |
| `--no-auto-resolve-git-submodules` | Disable automatic git submodule resolution and mounting |
| `--print-overlays` | Print each overlay group and its settings, then exit |
| `--x11` | Enable X11 forwarding (DISPLAY + `/tmp/.X11-unix` socket) |
| `--podman-remote` | Podman socket passthrough into container |
| `--shell SHELL` | Shell to use inside container (e.g. `bash`, `zsh`) |
| `--login` / `--no-login` | Run shell as login shell (sources `/etc/profile`). `--no-login` explicitly disables. |
| `--prompt-banner TEXT` | Custom prompt banner text |
| `--auto-attach` | Auto attach to named container if already running |
| `--auto-replace` | Auto replace named container if already exists |
| `--export SRC:DST[:0]` | Export container path to host (requires `--user-overlay`). Append `:0` for copy-only. May be repeated. |
| `--fuse-overlayfs` | Use fuse-overlayfs for overlay mount program (see [Fuse-Overlayfs](overlays.md#fuse-overlayfs)) |

All unrecognized flags are passed through to `podman run` directly.

## Overlay Implication Chain

```
adhoc → session → host + interactive + dotfiles → user
```

Each overlay implies its prerequisites. `--adhoc` activates all overlays.
See [Overlays](overlays.md) for details on each group.

## Config Precedence

```
CLI > config-script > devcontainer.json > ~/.podrunrc*
```

Scalar values use first-set-wins from left to right. Exports append in order:
`rc + dc + script + cli`. See [Configuration](configuration.md).

## `customizations.podrun` Keys

Keys in `customizations.podrun` of `devcontainer.json`:

| JSON Key | Type | Equivalent Flag |
|----------|------|-----------------|
| `userOverlay` | bool | `--user-overlay` |
| `hostOverlay` | bool | `--host-overlay` |
| `interactiveOverlay` | bool | `--interactive-overlay` |
| `session` | bool | `--session` |
| `adhoc` | bool | `--adhoc` |
| `dotFilesOverlay` | bool | `--dotfiles` |
| `x11` | bool | `--x11` |
| `podmanRemote` | bool | `--podman-remote` |
| `shell` | string | `--shell` |
| `login` | bool | `--login` |
| `promptBanner` | string | `--prompt-banner` |
| `autoAttach` | bool | `--auto-attach` |
| `autoReplace` | bool | `--auto-replace` |
| `fuseOverlayfs` | bool | `--fuse-overlayfs` |
| `noAutoResolveGitSubmodules` | bool | `--no-auto-resolve-git-submodules` |
| `exports` | list | `--export` |
| `noPodrunrc` | bool | `--no-podrunrc` |
| `localStore` | string | `--local-store` |
| `localStoreAutoInit` | bool | `--local-store-auto-init` |
| `localStoreIgnore` | bool | `--local-store-ignore` |
| `storageDriver` | string | `--storage-driver` (podman global) |
| `configScript` | string or list | `--config-script` |

## Top-Level Devcontainer Fields

| Field | Behavior |
|-------|----------|
| `image` | Fallback image when no CLI image is given |
| `workspaceFolder` | Container working directory (default `/app`) |
| `workspaceMount` | Custom workspace mount string (target overrides `workspaceFolder`) |
| `containerEnv` | Environment variables set in the container |
| `mounts` | Additional bind/volume mounts (string or object form) |
| `runArgs` | Extra podman run args |
| `capAdd` | Capabilities to add |
| `securityOpt` | Security options |
| `privileged` | Run as privileged |
| `init` | Use `--init` |

## Environment Variables

### Host-read

| Variable | Description |
|----------|-------------|
| `PODRUN_PODMAN_PATH` | Override the podman binary path (highest priority, checked before any parsing) |
| `PODRUN_UID` | Override UID on Windows (default: 1000) |
| `PODRUN_GID` | Override GID on Windows (default: 1000) |

### Container-exported (always)

Set in every podrun container:

| Variable | Description |
|----------|-------------|
| `PODRUN_CONTAINER` | Marker (`1`) indicating execution inside a podrun container |
| `PODRUN_OVERLAYS` | Comma-separated list of active overlay tokens (e.g. `user,host,interactive,dotfiles,session`) |

### Container-exported (on demand)

Set when the relevant overlay or option is active:

| Variable | Description |
|----------|-------------|
| `PODRUN_WORKDIR` | Workspace folder path (host overlay) |
| `PODRUN_SHELL` | Shell override |
| `PODRUN_LOGIN` | Login shell flag (`1` or `0`) |
| `PODRUN_IMG` | Full image reference |
| `PODRUN_IMG_NAME` | Image name component |
| `PODRUN_IMG_REPO` | Image repo component |
| `PODRUN_IMG_TAG` | Image tag component |
| `PODRUN_ALT_ENTRYPOINT` | User `--entrypoint` override (extracted and passed as env) |
| `PODRUN_PODMAN_REMOTE` | Podman remote mode active |
| `PODRUN_DEVCONTAINER_CLI` | Invoked by devcontainer CLI |

### Exec-session

| Variable | Description |
|----------|-------------|
| `PODRUN_STTY_INIT` | Terminal size for exec attach sessions |

## Container Lifecycle

When a `--name` is provided (or derived from the image), podrun checks for
existing containers:

- **Running container**: prompts to attach or replace (or use `--auto-attach`
  / `--auto-replace` to skip the prompt)
- **Stopped container**: prompts to replace (or use `--auto-replace`)

## Subcommand Passthrough

Podrun transparently proxies podman subcommands it does not enhance. Commands
like `ps`, `inspect`, `pull`, `build`, `version`, `exec`, `events`, `stop`,
and `rm` are forwarded directly to podman:

```bash
podrun ps -a                    # → podman ps -a
podrun inspect mycontainer      # → podman inspect mycontainer
podrun version --format json    # → podman version --format json
```

This makes podrun a drop-in replacement for podman in tools that expect a
Docker/Podman-compatible CLI (e.g. the devcontainer CLI).

## Shell Completion

Podrun wraps podman's built-in Cobra completion engine, giving full podman
completion with podrun flags layered on top.

**Bash** — add to `~/.bashrc`:

```bash
eval "$(podrun --completion bash)"
```

**Zsh** — add to `~/.zshrc`:

```bash
eval "$(podrun --completion zsh)"
```

**Fish** — add to `~/.config/fish/config.fish`:

```fish
podrun --completion fish | source
```

## Nested Podrun

Running podrun inside a podrun container is supported. The inner podrun
detects nesting via `PODRUN_CONTAINER=1` (set in every podrun container) and
automatically uses `podman-remote` to talk to the host daemon. Incompatible
global flags (e.g. `--root`, `--storage-driver`) are filtered silently based
on the remote binary's scraped flag set.

## Podman Flag Compatibility

Podrun scrapes `podman --help` and `podman run --help` at runtime to discover
available flags. Results are cached per podman version under
`$XDG_CACHE_HOME/podrun/` (Linux) or `%LOCALAPPDATA%/podrun/` (Windows).
Separate cache files are maintained for `podman` and `podman-remote` since
they expose different flag sets.
