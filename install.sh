#!/usr/bin/env bash
# ============================================================
#  Aria Code — One-command installer (uv-powered)
#  Usage:
#    bash install.sh             # full install (recommended)
#    bash install.sh --core      # slim core only (no file/cn/crypto/charts)
#    bash install.sh --dev       # everything incl. brokers + dev tools
#    bash install.sh --upgrade   # upgrade all installed packages
#    bash install.sh --no-wizard # skip setup wizard at the end
#
#  Dependencies come from pyproject.toml (single source of truth):
#    (no flag)  →  .[full]   core + cn + crypto + charts + data + files
#    --core     →  .         slim core only
#    --dev      →  .[all]    full + brokers + backtest + dev
# ============================================================
set -euo pipefail

# ── Colour helpers ────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'
info()  { echo -e "${CYAN}[aria]${NC} $*"; }
ok()    { echo -e "${GREEN}[aria] ✓${NC} $*"; }
warn()  { echo -e "${YELLOW}[aria] ⚠${NC} $*"; }
err()   { echo -e "${RED}[aria] ✗${NC} $*" >&2; }
step()  { echo -e "\n${BOLD}$*${NC}"; }
dim()   { echo -e "${DIM}$*${NC}"; }

# ── Banner ────────────────────────────────────────────────────
echo -e "
${CYAN}    _         _
   / \\  _ __ (_) __ _
  / _ \\| '__|| |/ _\` |
 / ___ \\  |  | | (_| |
/_/   \\_\\_|  |_|\\__,_|${NC}

  ${BOLD}Aria Code${NC} ${DIM}— AI Financial Terminal${NC}
"

CLI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${ARIA_VENV:-$CLI_DIR/.venv}"
BIN_DIR="${ARIA_BIN_DIR:-$HOME/.local/bin}"
LINK_PATH="$BIN_DIR/aria-code"
ALIAS_PATH="$BIN_DIR/aria"
PYTHON="${ARIA_PYTHON:-python3}"

# ── Parse flags ───────────────────────────────────────────────
MODE="full"
UPGRADE=0
NO_WIZARD=0
REBUILD=0
for arg in "$@"; do
    case "$arg" in
        --core)    MODE="core" ;;
        --dev)     MODE="dev" ;;
        --upgrade) UPGRADE=1 ;;
        --rebuild) REBUILD=1 ;;
        --no-wizard) NO_WIZARD=1 ;;
        --help|-h)
            echo "Usage: bash install.sh [--core|--dev|--upgrade|--rebuild|--no-wizard]"
            echo "  (no flag)    Full install: core + data sources + files + charts"
            echo "  --core       Slim: CLI + yfinance only (pip install aria-code)"
            echo "  --dev        Full + brokers + backtest + pytest/dev tools"
            echo "  --upgrade    Upgrade all existing packages"
            echo "  --rebuild    Remove the existing venv and recreate it (fixes Python drift"
            echo "               reported by /doctor after a system Python upgrade)"
            echo "  --no-wizard  Skip interactive setup wizard"
            exit 0 ;;
        *) warn "Unknown flag: $arg (ignored)" ;;
    esac
done

# Map mode → pyproject extra
case "$MODE" in
    core) EXTRA="" ;;
    dev)  EXTRA="all" ;;
    *)    EXTRA="full" ;;
esac

# ── Rebuild: drop the existing venv so it is recreated below ──
# Guard: only delete a directory that actually looks like a virtualenv —
# ARIA_VENV could point anywhere, and rm -rf on a wrong path is unrecoverable.
if [[ "$REBUILD" == "1" && -d "$VENV_DIR" ]]; then
    if [[ -f "$VENV_DIR/pyvenv.cfg" ]]; then
        info "Rebuilding virtual environment: $VENV_DIR"
        rm -rf "$VENV_DIR"
        ok "Old venv removed — a fresh one will be created with the current Python"
    else
        err "--rebuild: $VENV_DIR has no pyvenv.cfg — not a virtualenv, refusing to delete"
        exit 1
    fi
fi

# ── China mirror support (avoids GitHub / PyPI timeouts) ──────
# Opt in with ARIA_CN=1. Only then: switching where packages and a Python
# interpreter come from is the user's decision, not a silent retry — this used
# to kick in automatically on any failed download, so a brief network error
# anywhere routed the whole install through third-party mirrors.
#
# The Python builds come from npmmirror (Alibaba's mirror of the official
# python-build-standalone releases, same layout); uv still checks each build
# against the SHA-256 it ships with. They came through ghfast.top before, a
# public GitHub proxy whose operator is unknown.
ARIA_CN="${ARIA_CN:-0}"
CN_PYPI="https://pypi.tuna.tsinghua.edu.cn/simple"
CN_PY_REPO="https://registry.npmmirror.com/-/binary/python-build-standalone"
enable_cn_mirror() {
    export UV_DEFAULT_INDEX="${UV_DEFAULT_INDEX:-$CN_PYPI}"
    export UV_PYTHON_INSTALL_MIRROR="${UV_PYTHON_INSTALL_MIRROR:-$CN_PY_REPO}"
    export PIP_INDEX_URL="${PIP_INDEX_URL:-$CN_PYPI}"
    ARIA_CN=1
    info "China mirrors enabled (PyPI Tsinghua + Python build mirror)"
}
[[ "$ARIA_CN" == "1" ]] && enable_cn_mirror

# ── Step 1: package manager (uv preferred) ────────────────────
step "1 / 5  Setting up package manager"
USE_UV=0
if command -v uv &>/dev/null; then
    ok "uv found: $(uv --version 2>/dev/null || echo uv)"
    USE_UV=1
else
    info "Installing uv (fast Python package manager)…"
    if command -v curl &>/dev/null && curl -LsSf https://astral.sh/uv/install.sh | sh; then
        # uv installs to ~/.local/bin (or ~/.cargo/bin on older versions)
        export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    fi
    if command -v uv &>/dev/null; then
        ok "uv installed: $(uv --version 2>/dev/null || echo uv)"
        USE_UV=1
    else
        warn "uv unavailable — falling back to python venv + pip"
    fi
fi

# ── Step 2: virtual environment ───────────────────────────────
step "2 / 5  Creating virtual environment"
if [[ "$USE_UV" -eq 1 ]]; then
    # uv downloads a managed CPython if no suitable interpreter (≥3.10) exists,
    # so there's no "please install Python first" prerequisite.
    if [[ ! -d "$VENV_DIR" ]]; then
        if ! uv venv "$VENV_DIR" --python 3.12 --seed 2>/dev/null; then
            # Python download may have timed out: fall back to any local Python.
            if [[ "$ARIA_CN" != "1" ]]; then
                warn "Python download failed. In mainland China, re-run with ARIA_CN=1 to use mirrors."
            fi
            uv venv "$VENV_DIR" --seed
        fi
        ok "Virtual environment created (uv)"
    else
        ok "Virtual environment exists: $VENV_DIR"
    fi
else
    if ! command -v "$PYTHON" &>/dev/null; then
        err "Neither uv nor python3 is available."
        echo -e "  Install uv:    ${CYAN}curl -LsSf https://astral.sh/uv/install.sh | sh${NC}"
        echo -e "  Or install Python 3.10+ from https://python.org and re-run."
        exit 1
    fi
    PY_VER=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    PY_MAJOR=$("$PYTHON" -c "import sys; print(sys.version_info.major)")
    PY_MINOR=$("$PYTHON" -c "import sys; print(sys.version_info.minor)")
    if [[ "$PY_MAJOR" -lt 3 ]] || [[ "$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 10 ]]; then
        err "Python $PY_VER found, but Aria Code requires Python 3.10+."
        exit 1
    fi
    ok "Python $PY_VER"
    if [[ ! -d "$VENV_DIR" ]]; then
        "$PYTHON" -m venv "$VENV_DIR"
        ok "Virtual environment created (venv)"
    else
        ok "Virtual environment exists: $VENV_DIR"
    fi
fi

VENV_PY="$VENV_DIR/bin/python"

# ── Step 3: dependencies (from pyproject.toml) ────────────────
step "3 / 5  Installing dependencies"
if [[ -n "$EXTRA" ]]; then
    dim "  target: aria-code[$EXTRA]  (editable)"
    TARGET="${CLI_DIR}[$EXTRA]"
else
    dim "  target: aria-code  (slim core, editable)"
    TARGET="${CLI_DIR}"
fi

install_pkgs() {
    local target="$1"
    if [[ "$USE_UV" -eq 1 ]]; then
        uv pip install --python "$VENV_PY" ${UPGRADE:+--upgrade} -e "$target"
    else
        "$VENV_DIR/bin/pip" install --quiet --upgrade pip
        "$VENV_DIR/bin/pip" install ${UPGRADE:+--upgrade} -e "$target"
    fi
}

if install_pkgs "$TARGET"; then
    ok "Dependencies installed"
else
    if [[ "$ARIA_CN" != "1" ]]; then
        warn "Install failed. In mainland China, re-run with ARIA_CN=1 to use mirrors."
    fi
    warn "Full install failed — retrying with slim core so the CLI still works…"
    if install_pkgs "$CLI_DIR"; then
        ok "Core installed (some optional features unavailable — use /install later)"
    else
        err "Dependency install failed. Try: $VENV_DIR/bin/pip install -e \"$TARGET\""
        exit 1
    fi
fi

# ── Step 4: Ollama (local LLM) ────────────────────────────────
step "4 / 5  Checking Ollama (local LLM runtime)"
if command -v ollama &>/dev/null; then
    OLLAMA_VER=$(ollama --version 2>/dev/null | head -1 || echo "unknown")
    ok "Ollama installed: $OLLAMA_VER"
    MODELS=$(ollama list 2>/dev/null | tail -n +2 | awk '{print $1}' | tr '\n' '  ' || true)
    if [[ -n "$MODELS" ]]; then
        dim "  Available models: $MODELS"
    else
        warn "No models downloaded yet."
        dim "  Recommended: ollama pull qwen2.5:7b"
    fi
else
    warn "Ollama not found — local LLM features will be unavailable"
    dim "  Install: https://ollama.com/download"
    SYSTEM="$(uname -s)"
    if [[ "$SYSTEM" == "Darwin" ]]; then
        if command -v brew &>/dev/null; then
            read -r -p "  Install Ollama via Homebrew? [y/N] " INSTALL_OLLAMA
            if [[ "${INSTALL_OLLAMA:-N}" =~ ^[Yy]$ ]]; then
                brew install ollama && ok "Ollama installed" || warn "Brew install failed — install manually"
            fi
        else
            dim "  Run: curl -fsSL https://ollama.com/install.sh | sh"
        fi
    elif [[ "$SYSTEM" == "Linux" ]]; then
        read -r -p "  Install Ollama now? [y/N] " INSTALL_OLLAMA
        if [[ "${INSTALL_OLLAMA:-N}" =~ ^[Yy]$ ]]; then
            curl -fsSL https://ollama.com/install.sh | sh && ok "Ollama installed" || warn "Install failed — try manually"
        fi
    fi
fi

# ── Step 5: CLI launcher ──────────────────────────────────────
step "5 / 5  Registering aria-code launcher"
mkdir -p "$BIN_DIR"

ENTRYPOINT="$VENV_DIR/bin/aria-code"   # created by the editable install's [project.scripts]
LAUNCHER="$CLI_DIR/aria-code"
if [[ -x "$ENTRYPOINT" ]]; then
    ln -sf "$ENTRYPOINT" "$LINK_PATH"
    ln -sf "$ENTRYPOINT" "$ALIAS_PATH"
elif [[ -f "$LAUNCHER" ]]; then
    chmod +x "$LAUNCHER"
    ln -sf "$LAUNCHER" "$LINK_PATH"
    ln -sf "$LAUNCHER" "$ALIAS_PATH"
else
    cat > "$VENV_DIR/bin/aria-code-launcher" <<EOF
#!/bin/bash
exec "$VENV_DIR/bin/python" -m aria_code.apps.cli.main "\$@"
EOF
    chmod +x "$VENV_DIR/bin/aria-code-launcher"
    ln -sf "$VENV_DIR/bin/aria-code-launcher" "$LINK_PATH"
    ln -sf "$VENV_DIR/bin/aria-code-launcher" "$ALIAS_PATH"
fi
ok "Launchers → $LINK_PATH, $ALIAS_PATH"

# ── PATH check ────────────────────────────────────────────────
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    warn "$BIN_DIR is not in your PATH."
    echo
    echo -e "  Add this to your ${BOLD}~/.zshrc${NC} or ${BOLD}~/.bashrc${NC}:"
    echo -e "  ${CYAN}export PATH=\"\$HOME/.local/bin:\$PATH\"${NC}"
    echo
fi

# ── Verify installation ───────────────────────────────────────
echo
info "Verifying installation..."
"$VENV_PY" -c "
import sys
ok = []
fail = []
checks = [
    ('rich',           'import rich'),
    ('aiohttp',        'import aiohttp'),
    ('prompt_toolkit', 'import prompt_toolkit'),
    ('yfinance',       'import yfinance'),
    ('pandas',         'import pandas'),
    ('numpy',          'import numpy'),
]
if '${MODE}' != 'core':
    checks += [
        ('akshare',       'import akshare'),
        ('pdfplumber',    'import pdfplumber'),
        ('duckdb',        'import duckdb'),
        ('mplfinance',    'import mplfinance'),
    ]
for name, stmt in checks:
    try:
        exec(stmt)
        ok.append(name)
    except ImportError:
        fail.append(name)

print(f'  ✓ {len(ok)} packages OK' + (f'  ✗ {len(fail)} missing: {fail}' if fail else ''))
sys.exit(1 if fail else 0)
" && VERIFY_OK=1 || VERIFY_OK=0

# ── Summary ───────────────────────────────────────────────────
echo
echo -e "╔════════════════════════════════════════╗"
if [[ "$VERIFY_OK" -eq 1 ]]; then
    echo -e "║  ${GREEN}${BOLD}Aria Code installed successfully!${NC}       ║"
else
    echo -e "║  ${YELLOW}${BOLD}Install complete (some warnings above)${NC}  ║"
fi
echo -e "╚════════════════════════════════════════╝"
echo
echo -e "  ${BOLD}Quick start:${NC}"
echo -e "  ${CYAN}aria-code${NC}                   # interactive REPL"
echo -e "  ${CYAN}aria-code -p \"AAPL 分析\"${NC}    # one-shot query"
echo -e "  ${CYAN}aria-code --help${NC}             # all options"
echo
echo -e "  ${BOLD}Add a feature later:${NC} ${DIM}(if you used --core)${NC}"
echo -e "  ${CYAN}$VENV_DIR/bin/pip install -e \"$CLI_DIR[files]\"${NC}   # /file commands"
echo -e "  ${CYAN}$VENV_DIR/bin/pip install -e \"$CLI_DIR[cn]\"${NC}      # A-share data"
echo
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo -e "  ${YELLOW}Remember to add $BIN_DIR to your PATH!${NC}"
    echo
fi

# ── Setup wizard ──────────────────────────────────────────────
if [[ "$NO_WIZARD" -eq 0 ]]; then
    echo -e "  ${DIM}(run with --no-wizard to skip)${NC}"
    read -r -p "  Run first-time setup wizard now? [Y/n] " RUN_WIZARD
    if [[ "${RUN_WIZARD:-Y}" =~ ^[Yy]$ ]] || [[ -z "${RUN_WIZARD:-}" ]]; then
        echo
        "$VENV_PY" -m aria_code.setup_wizard
    else
        echo
        dim "  You can run the wizard later: python3 -m aria_code.setup_wizard"
        dim "  Or set up manually: cp .env.daemon.template ~/.aria/.env && edit it"
    fi
fi
