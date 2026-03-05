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
python3 -m podrun [GLOBAL_OPTIONS] run [RUN_OPTIONS] IMAGE [COMMAND...]
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
podrun [GLOBAL_OPTIONS] run [RUN_OPTIONS] [PODMAN_OPTIONS] IMAGE [COMMAND...]
podrun [GLOBAL_OPTIONS] run [RUN_OPTIONS] [PODMAN_OPTIONS] -- [COMMAND...]
```

Podrun accepts all `podman run` flags alongside its own. Any unrecognized flags
are passed through to podman directly. Use `podrun run --help` to see both
podrun and podman options together. Use `podrun --help` for global options and
available commands.

### Examples

Ad-hoc container (auto-removes on exit):

```bash
podrun run --adhoc ubuntu:24.04
```

Persistent workspace (image survives exit):

```bash
podrun run --workspace ubuntu:24.04
```

Non-interactive command execution:

```bash
podrun run --host-overlay ubuntu:24.04 -- make -j8
```

Use zsh as the default shell:

```bash
podrun run --adhoc --shell zsh ubuntu:24.04
```

Run with a login shell (sources `/etc/profile`):

```bash
podrun run --adhoc --login ubuntu:24.04
```

Dry run (print the podman command without executing):

```bash
podrun run --adhoc --print-cmd ubuntu:24.04
```

Named container with auto-attach:

```bash
podrun run --workspace --name mydev --auto-attach ubuntu:24.04
```

Pass extra podman flags through:

```bash
podrun run --adhoc --gpus all -v /data:/data:ro ubuntu:24.04
```

Export container directories to the host:

```bash
# Export container's /opt/sdk/bin to ./local-sdk on the host
podrun run --user-overlay --export /opt/sdk/bin:./local-sdk ubuntu:24.04

# Multiple exports
podrun run --user-overlay --export /opt/sdk/bin:./sdk --export /usr/share/data:./data ubuntu:24.04
```

## Documentation

| Topic | Description |
|-------|-------------|
| [Overlays](docs/overlays.md) | Overlay groups, exports (reverse volumes), fuse-overlayfs |
| [devcontainer.json](docs/devcontainer.md) | Devcontainer fields, config scripts, devcontainer CLI |
| [Podrun Store](docs/store.md) | Project-local storage, auto-discovery, inline `--store` |
| [Testing](docs/testing.md) | Test setup, markers, parallel execution, test images |
| [Reference](docs/reference.md) | Full run options table, container lifecycle, subcommand passthrough, shell completion |

## Requirements

- Python >= 3.8
- Podman (rootless)

## License

[MIT](LICENSE.md)
