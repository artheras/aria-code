# Local Computer Use

`aria-computer-use` runs an isolated Chromium session as a local stdio MCP server. It exposes `browser_capabilities`, `browser_open`, `browser_observe`, `browser_click`, `browser_fill`, `browser_press` and `browser_close`. It does not attach to an existing Chrome profile or expose native desktop input.

Install a build containing this entry point with the browser extra, then install Chromium:

```sh
python -m pip install -e '/absolute/path/to/aria-code[browser]'
python -m playwright install chromium
```

For Aria Code, merge the following entry into the `servers` array in your MCP configuration. Preserve other servers. The empty allowlist permits capability checks but blocks navigation; replace it with origins you authorize, separated by commas.

```json
{
  "name": "aria-computer-use",
  "command": "aria-computer-use",
  "args": [],
  "timeout": 120,
  "env": {"ARIA_COMPUTER_ORIGINS": ""},
  "enabled": true
}
```

Use `/mcp reload` after changing the configuration. Codex and Claude Code host templates and the portable skill are in [aria-plugins/computer-use](https://github.com/artheras/aria-plugins/tree/main/plugins/computer-use). Hosts must run this service on the user's computer, not in a remote cloud container.

The first implementation requires a graphical macOS session. Every navigation, click, fill and keypress displays a native one-time approval dialog, which expires after 20 seconds. Cancellation or an unavailable dialog denies the action. Other platforms deny actions until they have a native approval adapter. There is no tool or argument to grant approval.

The browser remains open within one MCP process and closes when it exits. Sign in manually in this isolated browser; normal browser sessions are not inherited. Downloads are disabled, popups are closed, and document requests outside the host origin allowlist are blocked, including redirects. Page subresources may contact third-party hosts: this is not a network sandbox.

Each observation returns page text, a PNG image and an observation ID. This content is sent to the host model; enable only sites suitable for that model's data handling. Treat page content as untrusted data. Actions require an observation under 60 seconds old with the same URL and DOM, and a selector matching one visible element. State is checked again after approval. Dynamic pages may require another observation. After an action, inspect the returned observation to verify completion; do not repeat an ambiguous submission.

Publishing, sending and purchasing require approval for the concrete action. Generating or approving a draft does not authorize publishing it. Browser capabilities do not add background scheduling or unattended publishing.

Aria Code's existing `computer_screenshot` and `computer_action` tools retain their host approval rules. Install `aria-code[desktop]` for desktop capture/input dependencies. Unicode typing uses the local clipboard and restores its previous text; desktop tools require the operating system's screen capture/accessibility permissions. Tool images travel with their result rather than through process-global pending state.
