# Document Generation Architecture: Tier 1 (Go) vs Tier 2 (`arthera-doc-studio`)

`aria-code` uses a **two-tier document generation architecture** so the core CLI stays fast and zero-dependency while high-design deliverables can use a full LibreOffice + Poppler visual self-inspection loop.

## Tier 1: Built-in Zero-Dependency Go OOXML (`go/runtime`)

- **Tools**: `export_xlsx`, `export_docx`, `export_pptx`, `export_pdf`, `export_chart`
- **When to use**: Fast local generation inside `aria-code` with zero external runtime dependencies (no Python, no LibreOffice, no Docker).
- **Strengths**: Instant startup, single-binary distribution, native live formulas and `Audit_Provenance` in `.xlsx`, 16:9 `.pptx`, styled `.docx`.

## Tier 2: `arthera-doc-studio` (Standalone MCP + Docker Repo)

- **Repository**: `https://github.com/artheras/arthera-doc-studio` (cloned alongside `aria-code` as `../arthera-doc-studio` for automatic MCP discovery by `aria-native`)
- **When to use**: Board-ready decks, executive DOCX reports/template filling, institutional XLSX models requiring LibreOffice Calc recalculation audit, and **Claude-style Vision-in-the-loop QA** (`Generate → LibreOffice PDF → pdftoppm PNG → Inspect → Fix`).
- **Tools**:
  - `doc_studio_capabilities`
  - `doc_build_xlsx`
  - `doc_audit_xlsx`
  - `doc_build_pptx`
  - `doc_build_docx`
  - `doc_render_visual_qa`
- **Extraction readiness**: Packaged with its own `pyproject.toml`, `Dockerfile` (Python 3.12 + LibreOffice + Poppler + Noto CJK/Latin fonts), `skills/document-studio/SKILL.md`, and `tests/test_doc_studio.py` with zero imports from `aria_code`, matching `artheras/financial-service`.
