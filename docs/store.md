# Podman Local Storage (Podrun Store)

> Back to [README](../README.md) for install and quickstart.

`podrun store init` creates a project-local podman storage directory.
Podrun automatically injects `--root`/`--runroot`/`--storage-driver` flags
when a store is discovered, keeping all images, layers, and runtime state
local to the project without affecting your system podman.

## Auto-Discovery

Podrun automatically discovers project-local stores by walking upward from the
current directory looking for `.devcontainer/.podrun/store/graphroot/`. When
found, store flags (`--root`/`--runroot`/`--storage-driver`) are injected
automatically for all subcommands — `run`, `exec`, and passthrough (`ps`,
`images`, etc.):

```bash
podrun store init                    # creates .devcontainer/.podrun/store/
cd sub/dir                           # works from any subdirectory
podrun ps                            # auto-discovers and uses project store
podrun run --adhoc ubuntu:24.04      # auto-discovers for run too
podrun exec mycontainer ls           # auto-discovers for exec too
```

Auto-discovery has the lowest priority. Explicit `--store` and devcontainer.json
`store` key take precedence. If `--root`/`--runroot`/`--storage-driver` are
already present in global flags, discovery is silently skipped.

Use `--ignore-store` to bypass auto-discovery:

```bash
podrun --ignore-store ps            # uses system podman storage
```

`--ignore-store` only suppresses auto-discovery; explicit `--store` and
devconfig `store` still work when `--ignore-store` is set.

## Inline Usage (`--store`)

The `--store` flag explicitly resolves a store directory into podman global
flags, bypassing auto-discovery:

```bash
podrun store init                              # one-time setup
podrun --store .devcontainer/.podrun/store run --adhoc ubuntu:24.04  # use store directly
```

Use `--auto-init-store` to create the store on first use (no separate init
step):

```bash
podrun --store .devcontainer/.podrun/store --auto-init-store run --adhoc ubuntu:24.04
```

Use `--store-registry` to configure a registry mirror during auto-init:

```bash
podrun --store .devcontainer/.podrun/store --auto-init-store --store-registry mirror.example.com run --adhoc ubuntu:24.04
```

These flags can also be set in [devcontainer.json](devcontainer.md):

```jsonc
{
  "customizations": {
    "podrun": {
      "store": ".devcontainer/.podrun/store",
      "autoInitStore": true,
      "storeRegistry": "mirror.example.com"
    }
  }
}
```

`--store` conflicts with explicit `--root`/`--runroot`/`--storage-driver` in
global flags.

## Store Options

```bash
podrun store init --store-dir .devcontainer/.podrun/store  # custom directory (default: .devcontainer/.podrun/store)
podrun store init --registry mirror.example.com  # configure registry mirror
podrun store info     # show store paths and registry config
podrun store destroy  # remove store and its /tmp runroot
```

The runroot (runtime state) lives under `/tmp/podrun-stores/` to avoid NFS
issues and the 108-byte `sun_path` limit. A symlink at
`.devcontainer/.podrun/store/runroot` makes the relationship visible. After a
reboot, `--store` recreates the `/tmp` directory automatically.

---

See also: [Run Options](reference.md) for the full flag reference, [devcontainer.json](devcontainer.md) for config file support.
