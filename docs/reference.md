# Reference

> Back to [README](../README.md) for install and quickstart.

## Global Options

These flags apply to all subcommands (`run`, `exec`, passthrough) and must
appear before the subcommand:

| Flag | Description |
|---|---|
| `--store DIR` | Use project-local store directory (see [Podrun Store](store.md)) |
| `--ignore-store` | Suppress auto-discovery of project-local store |
| `--auto-init-store` | Auto-create store if missing (requires `--store`) |
| `--store-registry HOST` | Registry mirror for auto-init (requires `--store` + `--auto-init-store`) |

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
| `--podman-remote` | Podman socket passthrough. When a store is active, auto-starts a per-store `podman system service`; otherwise falls back to the systemd-managed socket. |
| `--prompt-banner TEXT` | Custom prompt banner text |
| `--auto-attach` | Auto attach to named container if already running |
| `--auto-replace` | Auto replace named container if already exists |
| `--print-cmd` / `--dry-run` | Print the podman command instead of executing |
| `--config PATH` | Explicit path to devcontainer.json |
| `--no-devconfig` | Skip devcontainer.json discovery |
| `--config-script PATH` | Run script and inline its stdout as args |
| `--fuse-overlayfs` | Use fuse-overlayfs for overlay mounts (see [Fuse-Overlayfs](overlays.md#fuse-overlayfs)) |
| `--check-flags` | Diff static podman flags against installed podman |
| `--completion SHELL` | Generate shell completion script (`bash`, `zsh`, `fish`) and exit |
| `--version` | Show version and exit |
| `-h` / `--help` | Show podman run help with podrun options |

## Container Lifecycle

When a `--name` is provided (or derived from the image), podrun checks for
existing containers:

- **Running container**: prompts to attach or replace (or use `--auto-attach`
  / `--auto-replace` to skip the prompt)
- **Stopped container**: prompts to replace (or use `--auto-replace`)

## Subcommand Passthrough

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

## Podman Flag Compatibility

Podrun maintains a static set of podman value flags to correctly parse
mixed argument lists. Use `--check-flags` to compare the static set against
your installed podman version and identify any flags that need updating.

## Shell Completion

Podrun provides shell completion that wraps podman's built-in Cobra completion
engine. This gives full podman completion (images, containers, flags) with
podrun-specific flags layered on top.

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

After reloading your shell, `podrun <TAB>` will complete subcommands, images,
container names, flags, and all other values that podman's completion supports.
