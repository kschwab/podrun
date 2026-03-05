# devcontainer.json

> Back to [README](../README.md) for install and quickstart.

Podrun discovers and reads `.devcontainer/devcontainer.json` from the current
directory (searching upward). Supported fields:

```jsonc
{
    "image": "ubuntu:24.04",
    "workspaceFolder": "/workspace",
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
            "store": ".devcontainer/.podrun/store",
            "autoInitStore": true,
            "storeRegistry": "mirror.example.com",
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

## Config Scripts

The `--config-script` flag runs a script and splices its stdout into the
argument list at the position where the flag appeared:

```bash
podrun run --host-overlay --config-script ./my-config.sh ubuntu:24.04
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

## Devcontainer CLI

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

---

See also: [Run Options](reference.md) for the full flag reference, [Store](store.md) for project-local storage.
