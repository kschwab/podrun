# Project-Local Storage

> Back to [README](../README.md) for install and quickstart.

Podrun can use a project-local podman storage directory, keeping all images,
layers, and runtime state isolated from your system podman.

## Flags

| Flag | Description |
|------|-------------|
| `--local-store DIR` | Use a specific store directory |
| `--local-store-ignore` | Suppress auto-discovery of project-local store |
| `--local-store-auto-init` | Auto-create store if missing (uses `--local-store` or auto-discovered path) |
| `--local-store-info` | Print store information and exit |
| `--local-store-destroy` | Remove project-local store before proceeding |

## Auto-Discovery

Podrun walks upward from the current directory looking for a project root.
When `.devcontainer/` is found, the store path is
`.devcontainer/.podrun/store/`. When `.devcontainer.json` is found instead,
the store path is `.podrun/store/`. If the store's `graphroot/` directory
exists, store flags (`--root`/`--runroot`/`--storage-driver`) are injected
automatically for all subcommands — `run`, passthrough (`ps`, `images`,
etc.):

```bash
# Create a store (one-time)
podrun --local-store .devcontainer/.podrun/store --local-store-auto-init run --adhoc ubuntu:24.04

# Works from subdirectories — auto-discovers the store
cd sub/dir
podrun ps
podrun run --adhoc ubuntu:24.04
```

Store resolution priority (highest to lowest):

1. `--local-store` CLI flag
2. `--config-script` output
3. `customizations.podrun.localStore` in devcontainer.json
4. `~/.podrunrc*` output
5. `PODRUN_LOCAL_STORE` environment variable
6. Auto-discovery (upward walk for `.devcontainer/.podrun/store` or `.podrun/store`)

If `--root`/`--runroot`/`--storage-driver` are already present in global
flags, discovery is silently skipped.

Use `--local-store-ignore` to bypass all store resolution:

```bash
podrun --local-store-ignore ps    # uses system podman storage
```

## Usage Examples

Create and use a project store:

```bash
podrun --local-store .devcontainer/.podrun/store --local-store-auto-init \
  run --adhoc ubuntu:24.04
```

Show store info:

```bash
podrun --local-store-info
```

Destroy a project store:

```bash
podrun --local-store-destroy
```

## devcontainer.json

Store settings can be specified in `customizations.podrun`:

```jsonc
{
  "customizations": {
    "podrun": {
      "localStore": ".devcontainer/.podrun/store",
      "localStoreAutoInit": true,
      "localStoreIgnore": false,
      "storageDriver": "overlay"
    }
  }
}
```

## Storage Layout

```
<project>/
  .devcontainer/.podrun/store/
    graphroot/                  # Podman storage root (images, layers)
    runroot → /tmp/podrun-stores/<hash>/   # Symlink to runtime state
```

The runroot lives under `/tmp/podrun-stores/` to avoid NFS issues and the
108-byte `sun_path` socket path limit. A symlink at `runroot` makes the
relationship visible. After a reboot, the `/tmp` directory is recreated
automatically on first use.

## Store Service

When `--podman-remote` and a local store are both active, podrun auto-starts a
`podman system service` daemon scoped to the store. The service listens on a
Unix socket under `/tmp/podrun-stores/<hash>/podman.sock` and is managed via a
PID file. The service is started idempotently — if already running, podrun
reuses the existing socket.

## Remote Clients

Project-local storage is not applicable when using `podman-remote` or on
Windows (where podman always operates as a remote client talking to a podman
machine VM). Store flags are silently skipped in these contexts.
`--local-store-info` reports "disabled (podman remote)" and
`--local-store-destroy` errors.

---

See also: [Reference](reference.md) for the full flag table,
[Configuration](configuration.md) for devcontainer.json support.
