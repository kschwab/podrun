# Getting Started

> Back to [README](../README.md) for install and quickstart.

This guide walks through your first podrun session. See
[Installing](../README.md#installing) if you haven't installed podrun yet.

## Your First Ad-Hoc Container

```bash
podrun run --adhoc ubuntu:24.04
```

This creates a disposable container with your host identity mapped in. When
you exit, the container is removed (`--adhoc` implies `--rm`).

Inside the container:

```
$ whoami
yourname
$ echo $HOME
/home/yourname
$ hostname
yourhostname
$ pwd
/app
```

Your host user, home directory, hostname, network, and current directory are
all available. Host dotfiles (`~/.vimrc`, `~/.gitconfig`, `~/.ssh`, etc.) are
mounted or copied in automatically.

## Persistent Sessions

For containers that survive exit, use `--session` with `--name`:

```bash
podrun run --session --name mydev ubuntu:24.04
```

Exit the container (`exit` or `Ctrl-D`), then re-attach:

```bash
podrun run --session --name mydev --auto-attach ubuntu:24.04
```

`--auto-attach` skips the "attach or replace?" prompt and connects to the
running container directly.

## Running Commands Non-Interactively

Pass the command after the image:

```bash
podrun run --host-overlay ubuntu:24.04 make -j8
```

`--host-overlay` maps your user identity and workspace without interactive
terminal flags. The container runs `make -j8` and exits.

## Using devcontainer.json

Create `.devcontainer.json` in your project root:

```jsonc
{
  "image": "ubuntu:24.04",
  "customizations": {
    "podrun": {
      "session": true,
      "name": "myproject",
      "autoAttach": true
    }
  }
}
```

Then run podrun with no image argument — it discovers the config automatically:

```bash
podrun run
```

See [Configuration](configuration.md) for all supported fields.

## Inspecting What Podrun Generates

**`--print-cmd`** shows the full podman command without executing it:

```bash
podrun --print-cmd run --adhoc ubuntu:24.04
```

This prints the exact `podman run` invocation with all generated flags,
entrypoint scripts, volumes, and environment variables.

**`--print-overlays`** shows how overlay groups break down:

```bash
podrun run --adhoc --print-overlays ubuntu:24.04
```

This displays each active overlay and the specific flags it contributes.

## Next Steps

- [Overlays](overlays.md) — overlay groups, dotfiles, exports, fuse-overlayfs
- [Configuration](configuration.md) — config merge, devcontainer.json, scripts
- [Reference](reference.md) — full flag tables, environment variables
