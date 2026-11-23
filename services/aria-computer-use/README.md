# Aria Computer Use (TypeScript / Playwright MCP)

First-class Node.js/TypeScript stdio MCP browser automation server for Aria Code, Claude Code, and Codex — zero Python runtime required.

## Capabilities (15 MCP Tools)
- **Observation & Navigation**: `browser_capabilities`, `browser_open`, `browser_observe` (in-page Set-of-Marks + virtual cursor HUD), `browser_scroll`, `browser_wait`, `browser_hover`, `browser_history`, `browser_tabs`
- **Human-Approved Mutations**: `browser_click`, `browser_fill`, `browser_press`, `browser_select`, `browser_batch` (1..10 steps under one native approval dialog)
- **Diagnostics & Recording**: `browser_export_recording`, `browser_close`, plus automatic console error, HTTP 4xx/5xx, and prompt-injection warnings.

## Quick Start

```bash
cd services/aria-computer-use
npm install
npx playwright install chromium
npm run build
npm test
```

Set `ARIA_COMPUTER_ORIGINS="https://example.com,http://localhost:3000"` in your host environment before starting.
