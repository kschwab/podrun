# podrun

A podman run superset with host identity overlays.

- Maps your host identity (user, home directory, dotfiles) into containers
- Reads `devcontainer.json` for reproducible project configs
- Passes unrecognized flags straight to podman — drop-in replacement

## Installing

### pip (from PyPI or GitHub)

```bash
python3 -m pip install podrun
```

Or install from GitHub directly:

```bash
python3 -m pip install git+https://github.com/kschwab/podrun@main
```

### Editable dev install

```bash
git clone https://github.com/kschwab/podrun && cd podrun
python3 -m pip install -e '.[dev]'
```

### Script only

```bash
wget -nv https://raw.githubusercontent.com/kschwab/podrun/main/podrun/podrun.py -O podrun && chmod a+x podrun
```

### Uninstalling

```bash
python3 -m pip uninstall podrun -y
```

## Quickstart

Ad-hoc container (auto-removes on exit):

```bash
podrun run --adhoc ubuntu:24.04
```

Persistent session:

```bash
podrun run --session --name mydev ubuntu:24.04
```

Non-interactive command execution:

```bash
podrun run --host-overlay ubuntu:24.04 make -j8
```

Shell override:

```bash
podrun run --adhoc --shell zsh ubuntu:24.04
```

Dry run (print the podman command without executing):

```bash
podrun --print-cmd run --adhoc ubuntu:24.04
```

Named container with auto-attach:

```bash
podrun run --session --name mydev --auto-attach ubuntu:24.04
```

## Documentation

| Topic | Description |
|-------|-------------|
| [Getting Started](docs/getting-started.md) | First session walkthrough |
| [Overlays](docs/overlays.md) | Overlay groups, dotfiles, exports, fuse-overlayfs |
| [Configuration](docs/configuration.md) | Config merge, devcontainer.json, scripts, podrunrc |
| [Local Store](docs/local-store.md) | Project-local podman storage |
| [Reference](docs/reference.md) | Flag tables, DC keys, env vars, completion |
| [Testing](docs/testing.md) | Contributor testing guide |

## Requirements

- Python >= 3.8
- Podman (rootless)
- Linux or Windows with [podman machine](https://docs.podman.io/en/latest/markdown/podman-machine.1.html)

## License

[MIT](LICENSE.md)
