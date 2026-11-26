# Aria Desktop Redesign Spec

> Bridges `Arthera/apps/desktop` (`nativeBridge.ts` -> `aria-native` JSONL / `mcp-serve`) with the unified Linear/Geist visual system (`assets/icons/aria-mark.svg`, `assets/icons/aria-icons.svg`).

## 1. Visual & Layout Architecture (3-Pane Workbench)

1. **Left Rail (240px, collapsible)**
   - Brand header using `aria-mark.svg` (20px monoline) + active workspace badge.
   - Mode switcher with 24px `1.5px` stroke icons:
     - **Chat / Build** (streaming JSONL turn + tool diffs)
     - **Packages (`pack`)** (`init`, `validate`, `test` scorecard, `export-mcp`)
     - **Connectors (`mcp-serve`)** (stdio / Streamable HTTP status, `skills/list`, `sql_query_readonly`)
     - **Audit & Usage** (`.aria/audit.jsonl` timeline + `pack usage` billing breakdown)
2. **Center Canvas (Fluid)**
   - Clean high-signal conversation stream (matching Claude Code / Codex restraint: collapsed tool pills, inline diff blocks, explicit approval cards).
   - **Approval Gate Card**: highlights write/shell/SQL target, actor (`ARIA_MCP_ACTOR`), and one-click `Approve` / `Deny` (synced with `go/relay` `/approvals/decision` when remote).
3. **Right Inspector / Artifact Preview (380px, toggleable)**
   - Live sandboxed preview for `pack promo` (`landing.html`, `PROMO.md`, `social_copy.md`) and generated `pptx`/`pdf` reports.
   - Live `pack test` Scorecard + `pack usage` breakdown (`by_actor`, `by_tool`, `approved_calls` vs `denied_calls`).

## 2. Design Tokens & Iconography

- **Surface Palette**: Neutral Zinc (`#09090B` / `#18181B` dark, `#FFFFFF` / `#FAFAFA` light) with Indigo accent (`#6366F1` / `#4F46E5`).
- **Programming Language Icons (Official Brand Colors)**:
  - Always render official recognizable language marks & brand colors (`Python` `#3776AB` / `#FFD43B`, `Go` `#00ADD8`, `Rust` `#DEA584` / `#CE422B`, `TypeScript` `#3178C6`) inside `assets/icons/aria-icons.svg` so developers recognize runtimes instantly.
- **Aria Service Icons (Redesigned Duotone Semantic Palette)**:
  - Keep 24×24 `1.5px` geometry from `assets/icons/aria-icons.svg` with distinct silhouettes and subtle semantic tint fills (`Computer Use` `#38BDF8`, `Finance` `#10B981`, `Healthcare` `#F43F5E`, `Spatial 3D` `#A78BFA`, `Bots` `#F59E0B`, `Security Vault` `#818CF8`, `Skills` `#2DD4BF`, `World` `#60A5FA`), plus `icon-mcp-http`, `icon-license-shield`, `icon-audit-usage`, and `icon-webhook-bot`.
- **Separation of Concerns**:
  - **Terminal (`aria-native`)**: zero heavy graphics; compact `✓` summaries and file paths only.
  - **Desktop & Web**: rich interactive cards, live HTML/artifact preview, license/billing dashboards, and visual brand assets.
