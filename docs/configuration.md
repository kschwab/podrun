# Configuration

> Back to [README](../README.md) for install and quickstart.

Podrun merges configuration from four sources. When the same key appears in
multiple sources, the highest-priority source wins.

## Config Precedence

```
CLI > config-script > devcontainer.json > ~/.podrunrc*
```

Scalar values (strings, booleans) use first-set-wins from left to right.
Exports are appended in order: `rc + dc + script + cli`.

## `~/.podrunrc*` (User Defaults)

A user-level config script that provides the lowest-priority defaults. Podrun
globs `~/.podrunrc*` and executes the single match. Its stdout is parsed as
flags, identical to `--config-script`.

Accepted file names: `.podrunrc`, `.podrunrc.sh`, `.podrunrc.py`,
`.podrunrc.bat`, etc. Directories are filtered out. If two or more files
match, podrun exits with an error — keep only one.

**Example** `~/.podrunrc.sh`:

```bash
#!/bin/bash
echo "--shell zsh --prompt-banner myhost"
```

**Opt-out:**

- CLI: `--no-podrunrc`
- devcontainer.json: `"noPodrunrc": true` in `customizations.podrun`

Either source suppresses `~/.podrunrc*` discovery.

## devcontainer.json

### Discovery

Podrun walks upward from the current directory looking for:

1. `.devcontainer/devcontainer.json`
2. `.devcontainer.json` (root-level shorthand)
3. `.devcontainer/<subfolder>/devcontainer.json` (named configurations)

Use `--devconfig PATH` to specify a path explicitly, or `--no-devconfig` to
skip discovery entirely.

### Top-Level Fields

| Field | Behavior |
|-------|----------|
| `image` | Fallback image when no CLI image is given |
| `workspaceFolder` | Container working directory (default `/app`) |
| `workspaceMount` | Custom workspace mount (target overrides `workspaceFolder`) |
| `containerEnv` | Environment variables set in the container |
| `remoteEnv` | Environment variables set in the container (merged with `containerEnv`; wins on conflict) |
| `mounts` | Additional bind/volume mounts (string or object form) |
| `runArgs` | Extra podman run args |
| `capAdd` | Capabilities to add |
| `securityOpt` | Security options |
| `privileged` | Run as privileged |
| `init` | Use `--init` |

Top-level fields are converted to podman flags at the lowest precedence level
within the devcontainer.json source.

### `customizations.podrun` Keys

| JSON Key | Type | Equivalent Flag |
|----------|------|-----------------|
| `name` | string | `--name` |
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
| `nfsRemediate` | string | `--nfs-remediate` |
| `nfsRemediatePath` | string | `--nfs-remediate-path` |

`customizations.podrun` values override top-level fields, and CLI flags
override both.

### Variable Expansion

Devcontainer.json variables are expanded in `workspaceFolder`,
`workspaceMount`, `mounts`, `runArgs`, `containerEnv`, `remoteEnv`, and
`customizations`.
Supported variables:

| Variable | Value |
|----------|-------|
| `${localWorkspaceFolder}` | Host path containing devcontainer.json |
| `${localWorkspaceFolderBasename}` | Basename of the host path |
| `${containerWorkspaceFolder}` | Resolved `workspaceFolder` value |
| `${containerWorkspaceFolderBasename}` | Basename of the container path |
| `${localEnv:VAR}` | Host environment variable (empty string if unset) |
| `${localEnv:VAR:default}` | Host environment variable with default |
| `${containerEnv:VAR}` | Left as-is (only available at container runtime) |
| `${devcontainerId}` | SHA-256 hash derived from `localWorkspaceFolder` |

### Full Example

```jsonc
{
  "image": "ubuntu:24.04",
  "workspaceFolder": "/workspace",
  "containerEnv": {
    "EDITOR": "vim"
  },
  "mounts": [
    "type=bind,source=/host/data,target=/data"
  ],
  "runArgs": ["--device-cgroup-rule=..."],
  "capAdd": ["SYS_PTRACE"],
  "securityOpt": ["seccomp=unconfined"],
  "init": true,
  "customizations": {
    "podrun": {
      "name": "mydev",
      "session": true,
      "shell": "zsh",
      "promptBanner": "my-project",
      "dotFilesOverlay": true,
      "exports": ["/opt/sdk/bin:./local-sdk"],
      "configScript": "./my-config.sh"
    }
  }
}
```

## Config Scripts (`--config-script`)

Config scripts are executables whose stdout is parsed as podrun/podman flags.
Script output follows the standard config precedence — CLI flags override
script flags, and script flags override devcontainer.json.

```bash
podrun run --config-script ./my-config.sh ubuntu:24.04
```

Where `my-config.sh` might output:

```
--host-overlay --shell zsh -e HTTP_PROXY=http://proxy.example.com:80
```

### Shebang Requirement

Config scripts must have a shebang line (`#!`) as their first line. Podrun
reads the shebang to determine the interpreter, resolves it on PATH, and
executes the script explicitly. This provides consistent behavior on both
Linux and Windows (where file extension associations are unreliable).

Supported forms:

| Shebang | Resolved as |
|---------|-------------|
| `#!/usr/bin/env python3` | `python3 script.py` (PATH lookup) |
| `#!/usr/bin/env -S python3 -u` | `python3 -u script.py` (flags preserved) |
| `#!/usr/bin/python3` | `python3 script.py` (basename fallback) |
| `#!C:\Python311\python.exe` | Direct path on Windows |

If the interpreter cannot be found on PATH, podrun exits with an error
showing the interpreter name and script path.

Multiple `--config-script` flags are executed left to right. When the same
flag appears in more than one script, the rightmost (last) value wins.

Config scripts can also be specified in devcontainer.json via `configScript`
(string or list of strings). Both DC and CLI scripts are executed, with CLI
scripts taking higher priority.

**Available environment variables** during script execution:

| Variable | Set When |
|----------|----------|
| `PODRUN_DEVCONTAINER_CLI` | Invoked via `devcontainer --docker-path podrun` |
| `PODRUN_PODMAN_REMOTE` | Resolved podman binary is `podman-remote` |

**Forbidden tokens:** Config scripts cannot output `--devconfig`,
`--config-script`, or `--no-devconfig`.

## Devcontainer CLI Integration

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

See also: [Reference](reference.md) for the full flag table,
[Overlays](overlays.md) for overlay groups.
