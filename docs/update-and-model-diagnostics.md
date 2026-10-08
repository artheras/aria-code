# Updates and model diagnostics

Startup checks only notify and run in the background. Installing an update is
an explicit action; a notice never replaces a running CLI.

```sh
aria --version
aria update --check
aria update
aria update --to 0.110.0
```

The updater selects published stable releases. Pip installs pin that version
after verifying it exists on PyPI, rather than selecting PyPI's historical
4.x line. The known historical releases 4.1.3–4.4.2 can migrate to the current
0.x line. This exception does not apply to hypothetical future major versions.
The check prints the executable path so an outdated installation on PATH can
be identified. Restart Aria after an update.

## Native downloads and recovery

On macOS and Linux, `aria update` retains interrupted downloads beneath
`~/.local/share/aria-code/downloads/`, or `$ARIA_CODE_HOME/downloads/`. A new
attempt requests the remaining byte range; a server ignoring that request
starts a fresh transfer. Downloads have bounded attempts and a total deadline,
and display progress in a terminal. A lock prevents concurrent downloads from
appending to the same file.

The updater validates GitHub's SHA-256 checksum before unpacking. If the GitHub
transfer fails, it tries the official `@artheras/aria-code-<platform>` npm
artifact of the same version and validates its SHA-512 integrity; npm itself is
not required for this fallback. An invalid checksum or unexpected archive path
stops installation. The new executable must report the expected version before
the command switches to it. Download and verification failures leave the old
executable in place.

After a successful native update from a managed release:

```sh
aria update --rollback
```

This validates and restores the previous managed executable without a network
request. Release directories are retained. If no managed previous release was
recorded, use `--to VERSION` or the installer. Pip/npm installs use their package
manager to select a version. Windows native installs continue to use
`scripts/install.ps1`; the resumable native updater and offline rollback above
apply to macOS/Linux.

The shell installer also retries, resumes interrupted transfers during retries,
and limits stalled downloads. Its temporary download files are removed on exit.
The persistent download cache and automatic npm artifact fallback belong to
`aria update`, not the shell installer.

## Check the model actually works

A successful service health response does not verify model generation or
function calling. Use an explicit probe for the currently selected route:

```sh
aria health --model
aria health --model --tools --json
aria health --model-id google/gemini-2.5-flash --model --timeout 45
```

The model override applies only to the probe. It does not change saved settings.
In an interactive session use `/health` for connection checks,
`/health --model` for text generation, or `/health --model --tools --json` for
text and tool protocol diagnostics.

The probe sends a short diagnostic prompt through the selected transport. It
does not read project files, use the agent's tools, or fall back to another
provider. `--tools` asks for one inert echo call, supplies its result, and verifies
that the model returns visible text afterward. It does not run a file/command
tool. This checks the protocol, not the model's ability to solve a project task.
These requests use the configured model account and can incur inference usage.

The JSON result uses schema `aria.model_probe.v1` and records `model`, `route`,
`provider`, `category`, `text_verified`, `tools_verified`, `duration_ms`, and
`first_token_ms`. The latter is null if text arrives only in the final event.
The standalone command exits 0 on success, 1 on failure, or 2 when no generation
probe was requested. The default total probe deadline is 30 seconds; `--timeout`
accepts 1–60. Expiration cancels the active request.

| Category | Next step |
| --- | --- |
| `auth` | Check credentials and project permissions; verify Google Cloud login/ADC. |
| `model_unavailable` | Check the model ID and serving region with `/model`. |
| `rate_limited` | Check quota or wait for the cooldown. |
| `timeout` | Check network/provider latency or increase the probe deadline. |
| `no_data` | Check the selected route's streaming/model configuration. |
| `protocol_unsupported` / `tool_protocol` | Check backend protocol support or use a direct provider for local tools. |

Reports omit raw provider error bodies and credentials. A 403 `generateContent`
error is an authentication/permission failure; a 404 model error is model
availability. Neither should be retried as a quota problem.
