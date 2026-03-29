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
| `initializeCommand` | Run on host before container creation (string, array, or object) |
| `onCreateCommand` | Run in container on first creation only (string, array, or object) |
| `postCreateCommand` | Run in container after `onCreateCommand`, first creation only (string, array, or object) |
| `postStartCommand` | Run in container on every start (string, array, or object) |
| `postAttachCommand` | Run in container on every exec attach (string, array, or object) |
| `updateContentCommand` | **Not supported** (warning printed; use devcontainer CLI) |
| `waitFor` | **Not supported** (warning printed; use devcontainer CLI) |

Top-level fields are converted to podman flags at the lowest precedence level
within the devcontainer.json source.

### Lifecycle Commands

Podrun supports a subset of devcontainer lifecycle commands. These run
setup scripts at specific points in the container's life.

| Command | Runs on | When | Frequency |
|---------|---------|------|-----------|
| `initializeCommand` | Host | Before container creation | Every `podrun run` |
| `onCreateCommand` | Container | During first-run entrypoint | Once (first creation) |
| `postCreateCommand` | Container | After `onCreateCommand` | Once (first creation) |
| `postStartCommand` | Container | After first-run setup completes | Every start |
| `postAttachCommand` | Container | When exec-ing into a running container | Every attach |

**Command forms** — all three devcontainer spec forms are accepted:

- **String**: `"npm install"` — run via `/bin/sh -c`
- **Array**: `["npm", "install"]` — direct invocation
- **Object**: `{"server": "npm start", "watch": "npm run watch"}` — named
  commands run in parallel (backgrounded with `&`, then `wait`)

**Devcontainer CLI guard** — when podrun is invoked via
`devcontainer --docker-path podrun`, lifecycle commands are skipped to avoid
double execution (the devcontainer CLI runs them itself). The guard checks
the `PODRUN_DEVCONTAINER_CLI` environment variable.

**Unsupported fields** — `updateContentCommand` and `waitFor` are not
executed. When present in devcontainer.json, podrun prints a single
consolidated warning suggesting the devcontainer CLI for full lifecycle
support.

**Example:**

```jsonc
{
  "image": "ubuntu:24.04",
  "initializeCommand": "echo 'preparing host...'",
  "onCreateCommand": ["npm", "install"],
  "postCreateCommand": "npm run build",
  "postStartCommand": {
    "server": "npm start",
    "watch": "npm run watch"
  },
  "postAttachCommand": "echo 'welcome back'",
  "customizations": {
    "podrun": {
      "session": true,
      "name": "mydev"
    }
  }
}
```

### `customizations.podrun` Keys

| JSON Key | Type | Equivalent Flag |
|----------|------|-----------------|
| `name` | string | `--name` |
| `userOverlay` | bool | [`--user-overlay`](reference.md#run-flags) |
| `hostOverlay` | bool | [`--host-overlay`](reference.md#run-flags) |
| `interactiveOverlay` | bool | [`--interactive-overlay`](reference.md#run-flags) |
| `session` | bool | [`--session`](reference.md#run-flags) |
| `adhoc` | bool | [`--adhoc`](reference.md#run-flags) |
| `dotFilesOverlay` | bool | [`--dotfiles`](reference.md#run-flags) |
| `x11` | bool | [`--x11`](reference.md#run-flags) |
| `podmanRemote` | bool | [`--podman-remote`](reference.md#run-flags) |
| `shell` | string | [`--shell`](reference.md#run-flags) |
| `login` | bool | [`--login`](reference.md#run-flags) |
| `promptBanner` | string | [`--prompt-banner`](reference.md#run-flags) |
| `autoAttach` | bool | [`--auto-attach`](reference.md#run-flags) |
| `autoReplace` | bool | [`--auto-replace`](reference.md#run-flags) |
| `fuseOverlayfs` | bool | [`--fuse-overlayfs`](reference.md#run-flags) |
| `noAutoResolveGitSubmodules` | bool | [`--no-auto-resolve-git-submodules`](reference.md#run-flags) |
| `exports` | list | [`--export`](reference.md#run-flags) |
| `noPodrunrc` | bool | [`--no-podrunrc`](reference.md#global-flags) |
| `localStore` | string | [`--local-store`](reference.md#global-flags) |
| `localStoreAutoInit` | bool | [`--local-store-auto-init`](reference.md#global-flags) |
| `localStoreIgnore` | bool | [`--local-store-ignore`](reference.md#global-flags) |
| `storageDriver` | string | `--storage-driver` (podman global) |
| `configScript` | string or list | [`--config-script`](reference.md#global-flags) |
| `nfsRemediate` | string | [`--nfs-remediate`](reference.md#global-flags) |
| `nfsRemediatePath` | string | [`--nfs-remediate-path`](reference.md#global-flags) |

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
  "onCreateCommand": "apt-get update && apt-get install -y build-essential",
  "postStartCommand": "echo 'ready'",
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

Config scripts are Python scripts whose stdout is parsed as podrun/podman flags.
Script output follows the standard config precedence — CLI flags override
script flags, and script flags override devcontainer.json.

```bash
podrun run --config-script ./my-config.py ubuntu:24.04
```

Where `my-config.py` might output:

```
--host-overlay --shell zsh -e HTTP_PROXY=http://proxy.example.com:80
```

Config scripts are always executed by the same Python interpreter that is
running podrun itself (`sys.executable`). No shebang line is required.

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
