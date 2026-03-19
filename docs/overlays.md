# Overlays

> Back to [README](../README.md) for install and quickstart.

Overlays are groups of podman flags that configure common container patterns.
Each overlay implies its prerequisites, so higher-level overlays automatically
activate the lower-level ones they depend on.

## Overview

| Flag | Description |
|------|-------------|
| `--user-overlay` | Map host user identity into container |
| `--host-overlay` | Host system context (implies `--user-overlay`) |
| `--interactive-overlay` | Interactive terminal session |
| `--dot-files-overlay` / `--dotfiles` | Mount host dotfiles (implies `--user-overlay`) |
| `--session` | Persistent named session (implies `--host-overlay` + `--interactive-overlay` + `--dotfiles`) |
| `--adhoc` | Disposable container (implies `--session` + `--rm`) |

## Implication Chain

```
adhoc → session → host + interactive + dotfiles → user
```

Using `--adhoc` activates every overlay. Using `--session` activates
everything except `--rm`. Using `--host-overlay` activates only itself and
`--user-overlay`.

`--dot-files-overlay` independently implies `--user-overlay` (dotfiles need
the user's home directory to exist in the container).

## User Overlay (`--user-overlay`)

Maps your host user identity into the container:

- `--userns=keep-id` — maps host UID/GID into the container
- Creates `/etc/passwd` and `/etc/group` entries for your user
- Creates your home directory
- Detects and configures your shell (falls back to `/bin/sh`)
- Installs passwordless `sudo` if available in the image
- Adds bootstrap capabilities (`CAP_DAC_OVERRIDE`, `CAP_CHOWN`, `CAP_FOWNER`,
  `CAP_SETPCAP`) during entrypoint setup, then drops them before running the
  user's shell
- Mounts entrypoint scripts into `/.podrun/`

```bash
podrun run --user-overlay ubuntu:24.04
# whoami → your username
# echo $HOME → /home/yourname
```

## Host Overlay (`--host-overlay`)

Implies `--user-overlay`. Overlays the host system context:

- Sets container hostname to the host hostname
- `--network=host` — shares host network namespace (skipped on Windows)
- `--security-opt=seccomp=unconfined`
- Mounts the current directory as the workspace (default: `/app`)
- Mounts `/etc/localtime` for timezone (if it exists)
- Auto-resolves and mounts git submodules (disable with
  `--no-auto-resolve-git-submodules`)

```bash
podrun run --host-overlay ubuntu:24.04 make -j8
# Builds in the container with host network and workspace mounted
```

## Interactive Overlay (`--interactive-overlay`)

Configures the container for interactive terminal use:

- `-it` — allocate pseudo-TTY and keep stdin open
- `--init` — use an init process (reaps zombies)
- `--detach-keys=ctrl-q,ctrl-q` — avoids accidental detach on Ctrl-P

```bash
podrun run --interactive-overlay --user-overlay ubuntu:24.04
```

## Dotfiles (`--dot-files-overlay` / `--dotfiles`)

Implies `--user-overlay`. Automatically included in `--session` and `--adhoc`.
Mounts select dotfiles from your host home directory into the container. Only
files that exist on the host are mounted.

**Mount-mode (read-only):** Files that are safe to share directly.

- `~/.emacs`
- `~/.emacs.d`
- `~/.vimrc`

**Copy-mode (writable):** Files that need to be writable inside the container.
Copied into the container at startup instead of bind-mounted.

- `~/.ssh` — needs writable access for agent sockets
- `~/.gitconfig` — credential helpers may write to it

```bash
podrun run --adhoc ubuntu:24.04
# ~/.vimrc is bind-mounted read-only
# ~/.ssh is copied in and writable
```

## Session Overlay (`--session`)

Implies `--host-overlay` + `--interactive-overlay` + `--dotfiles`. The
standard mode for persistent development containers.

Use `--name` for a stable container name, and `--auto-attach` /
`--auto-replace` for seamless reconnection:

```bash
# First run — creates the container
podrun run --session --name mydev ubuntu:24.04

# Later — auto-attaches to the running container
podrun run --session --name mydev --auto-attach ubuntu:24.04
```

## Ad-Hoc Overlay (`--adhoc`)

Implies `--session` + `--rm`. Disposable containers that are removed on exit.

```bash
podrun run --adhoc ubuntu:24.04
# Full session environment, container deleted on exit
```

## Exports (Reverse Volumes)

Normal `-v host:container` bind mounts mask the container's content with the
host directory. The `--export` flag goes the other direction: it copies
container-internal files to the host and symlinks the original path to a
host-mounted staging area.

**Syntax**: `--export container_path:host_path[:0]`

```bash
podrun run --user-overlay --export /opt/sdk/bin:./local-sdk my-image
```

The mechanism:

1. Podrun creates the host directory and bind-mounts it into the container at
   `/.podrun/exports/<hash>`
2. The entrypoint copies the container's original content into the staging area
   (skipped if the host directory is already non-empty)
3. The original container path is replaced with a symlink to the staging area

Both files and directories are supported. Non-existent container paths get a
symlink to the staging directory so that later writes are captured on the host.

**Copy-only mode** (`--export src:dst:0`): Appending `:0` skips the
rm/symlink step. Content is copied to the host but the original container path
is left intact. Use this for paths that contain bind-mounted files (e.g.
`/etc`) where the rm would fail.

Multiple exports:

```bash
podrun run --user-overlay \
  --export /opt/sdk/bin:./sdk \
  --export /usr/share/data:./data \
  ubuntu:24.04
```

Exports require `--user-overlay` (or any overlay that implies it).

**Config equivalent** in `customizations.podrun`:

```jsonc
{
  "customizations": {
    "podrun": {
      "exports": ["/opt/sdk/bin:./local-sdk"]
    }
  }
}
```

## Fuse-Overlayfs (`--fuse-overlayfs`)

On kernels that support native overlay idmap (`CONFIG_OVERLAY_FS_IDMAP`,
kernel 5.19+), `--userns=keep-id` is instant. On older kernels, podman falls
back to creating an ID-mapped copy of every image layer — which can hang for
minutes on large images.

The `--fuse-overlayfs` flag injects `--storage-opt
overlay.mount_program=/usr/bin/fuse-overlayfs`, bypassing the kernel
limitation via FUSE.

**When to use:**

- Container creation hangs with `--user-overlay` on large images
- Your kernel is older than 5.19 or lacks `CONFIG_OVERLAY_FS_IDMAP`
- `fuse-overlayfs` is installed on the host

**Performance:**

- **Container filesystem I/O**: ~0-5% overhead vs native overlay
- **Bind mount I/O** (`-v` host volumes): zero overhead (bypasses FUSE)

**`:O` overlay mount fallback:** Podrun automatically handles `:O` (overlay)
volume mounts regardless of whether `--fuse-overlayfs` is set. The fallback
priority is:

- `:0` suffix — always uses entrypoint copy-staging
- `:O` on a file — entrypoint copy-staging (overlay only works on directories)
- `:O` on a directory with fuse-overlayfs — native overlay
- `:O` on a directory without fuse-overlayfs — entrypoint copy-staging

## Inspecting Overlays

Use `--print-overlays` to see exactly what each active overlay group expands
to:

```bash
podrun run --adhoc --print-overlays ubuntu:24.04
```

---

See also: [Reference](reference.md) for the full flag table,
[Configuration](configuration.md) for devcontainer.json and config scripts.
