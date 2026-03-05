# Overlays

> Back to [README](../README.md) for install and quickstart.

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

## Exports (Reverse Volumes)

Normal `-v host:container` bind mounts mask the container's content with the
host directory. The `--export` flag goes the other direction: it copies
container-internal files to the host and symlinks the original path to the
host-mounted staging area.

```bash
podrun run --user-overlay --export /opt/sdk/bin:./local-sdk my-image
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

## Fuse-Overlayfs

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

---

See also: [Run Options](reference.md) for the full flag reference.
