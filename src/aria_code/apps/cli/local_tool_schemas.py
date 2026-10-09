"""本地工具的 JSON Schema 定义 —— 从 aria_cli.py 抽出（437 行）。

这 20 个 schema 是纯数据：描述本地工具（文件读写、命令执行、待办、浏览器、
经纪商查询等）暴露给模型的参数结构。原本内联在 aria_cli.py 第 1563-1999 行的
一个 `LOCAL_TOOL_SCHEMAS.extend([...])` 里。

抽出来的原因：它跟周围的代码没有任何耦合——那一段的上下文全是
`try: from x import y / LOCAL_TOOLS.update(...)` 这类**有执行顺序依赖**的注册
副作用，而 schema 本身只是字面量。混在一起既让文件变长，也让人误以为它们同样
敏感于顺序。

刻意做成函数而不是模块级常量：原文里两处依赖外部值——
  _todo_schema()  由调用方传入（它在 aria_cli 里定义，抽过来会带出更多依赖）
  CONFIG_DIR      只用于一句描述文案的插值
做成函数后这两个变成显式参数，模块本身零外部依赖，可独立测试。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

__all__ = ["build_local_tool_schemas"]


def build_local_tool_schemas(
    *,
    todo_schema: Dict[str, Any],
    config_dir: Any,
) -> List[Dict[str, Any]]:
    """返回本地工具的 schema 列表。

    Args:
        todo_schema: 待办工具的 schema（由 aria_cli._todo_schema() 产生）
        config_dir:  配置目录，仅用于 broker 参数的描述文案
    """
    CONFIG_DIR = config_dir          # 保持原文插值写法不变，减少搬运时的改动面
    return [
        todo_schema,
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file. Use this to understand existing code before modifying it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative file path"},
                    "offset": {"type": "integer", "description": "Start line (0-based), optional"},
                    "limit": {"type": "integer", "description": "Number of lines to read, optional"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_file",
            "description": (
                "Parse and analyze a local document, image, or video the user uploaded/referenced. "
                "Handles pdf, docx, xls/xlsx, csv/tsv, json, html, markdown, code, images "
                "(png/jpg/gif/webp/bmp), and video (mp4/mov/avi/mkv/webm — extracts metadata + "
                "keyframes for the vision model). Extracts text + metadata; images/keyframes are "
                "sent to the vision model so a vision-capable model can see them. Use this (not "
                "read_file) for non-plain-text files like PDFs, spreadsheets, screenshots, or clips."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to analyze"},
                    "max_chars": {"type": "integer", "description": "Max extracted text to return (default 6000; raise for long docs)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a new file or overwrite an existing file with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to write to"},
                    "content": {"type": "string", "description": "Complete file content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Edit a file by replacing old_string with new_string. Read the file first to get the exact "
                "text to replace. old_string must match exactly one place: include enough surrounding lines "
                "to make it unique, or set replace_all to change every match. To change a whole function, "
                "method or class, pass symbol instead of old_string (e.g. symbol=\"Session.refresh\"): "
                "new_string is then the complete new definition, or with position=\"after\" the code to "
                "insert after it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to edit"},
                    "old_string": {"type": "string", "description": "Exact text to find and replace (must match exactly)"},
                    "new_string": {"type": "string", "description": "Replacement text"},
                    "replace_all": {"type": "boolean", "description": "Replace every match instead of requiring exactly one (default false)"},
                    "symbol": {"type": "string", "description": "Instead of old_string: the definition to edit, by name or Class.method"},
                    "position": {"type": "string", "enum": ["replace", "after"], "description": "With symbol: replace the definition (default) or insert new_string after it"},
                },
                "required": ["path", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "multi_edit",
            "description": (
                "Apply several edits to ONE file in a single atomic operation — all succeed or "
                "none are applied. Use this instead of multiple edit_file calls when changing "
                "several spots in the same file. Edits apply in order; a later edit can match "
                "text a previous edit produced."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to edit"},
                    "edits": {
                        "type": "array",
                        "description": "Ordered list of edits to apply",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_string": {"type": "string", "description": "Exact text to find (must match exactly)"},
                                "new_string": {"type": "string", "description": "Replacement text"},
                                "replace_all": {"type": "boolean", "description": "Replace all occurrences (default false)"},
                            },
                            "required": ["old_string", "new_string"],
                        },
                    },
                },
                "required": ["path", "edits"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in a directory. Use glob patterns like '**/*.py' to filter.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path (default: current dir)"},
                    "pattern": {"type": "string", "description": "Glob pattern (default: *)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files in a directory (alias for list_files). Use glob patterns like '**/*.py' to filter.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path (default: current dir)"},
                    "pattern": {"type": "string", "description": "Glob pattern (default: *)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search for a regex pattern in source files. Returns matching lines with file paths and line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "path": {"type": "string", "description": "Directory to search in (default: current dir)"},
                    "glob": {"type": "string", "description": "File glob filter (default: **/*.py)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Search for a regex pattern in source files (alias for search_code).",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "path": {"type": "string", "description": "Directory to search in (default: current dir)"},
                    "glob": {"type": "string", "description": "File glob filter (default: **/*.py)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command. Use for: git, pip, python, pytest, ls, etc. For a server, watcher or anything that keeps running, set background=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                    "cwd": {"type": "string", "description": "Working directory (optional)"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (max 300, default 120). Use 180+ for data-heavy scripts."},
                    "background": {"type": "boolean", "description": "Start a long-running command (dev server, watcher, interactive program) and return at once with a process_id; use the process tool to read its output, send input or stop it"},
                    "wait_seconds": {"type": "number", "description": "With background: how long to wait for first output (default 3, max 30)"},
                    "until": {"type": "string", "description": "With background: return as soon as this regex appears in the output, e.g. 'Listening on'"},
                    "sandbox": {"type": "boolean", "description": "Set to true to securely execute the command inside an isolated Docker container instead of the host machine. Mandatory for running third-party or untested code."}
                },
                "required": ["command"],
            },
        },
    },
    # ── Extended tools ─────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "Fetch the text/content of any URL: web pages, GitHub files, "
                "documentation, API responses, PyPI pages. "
                "GitHub blob URLs are auto-converted to raw content. "
                "Use this to read docs, README files, or look up library APIs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url":       {"type": "string",  "description": "Full URL to fetch (https://)"},
                    "max_chars": {"type": "integer", "description": "Max characters to return (default 12000, max 40000)"},
                    "timeout":   {"type": "integer", "description": "Request timeout seconds (default 15, max 30)"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github",
            "description": (
                "GitHub integration via gh CLI. Actions: "
                "list_prs, list_issues, view_pr, view_issue, create_pr, "
                "list_commits, search, read_file, pr_diff, pr_checks. "
                "Use cwd to specify repo directory. "
                "Requires: gh CLI installed and authenticated (gh auth login)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action":  {"type": "string", "description": "Action to perform (list_prs|list_issues|view_pr|view_issue|create_pr|list_commits|search|read_file|pr_diff|pr_checks)"},
                    "number":  {"type": "integer","description": "PR or issue number (for view_pr, view_issue, pr_diff)"},
                    "title":   {"type": "string", "description": "PR title (for create_pr)"},
                    "body":    {"type": "string", "description": "PR body (for create_pr)"},
                    "branch":  {"type": "string", "description": "Head branch (for create_pr)"},
                    "base":    {"type": "string", "description": "Base branch (for create_pr, default main)"},
                    "state":   {"type": "string", "description": "Filter state: open|closed|all (default open)"},
                    "limit":   {"type": "integer","description": "Max results (default 20)"},
                    "q":       {"type": "string", "description": "Search query (for search action)"},
                    "kind":    {"type": "string", "description": "Search kind: code|issues|repos (default code)"},
                    "ref":     {"type": "string", "description": "File ref in owner/repo@branch:path format (for read_file)"},
                    "cwd":     {"type": "string", "description": "Working directory (git repo root)"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": (
                "Fast file-pattern search. Returns a sorted list of matching file paths. "
                "Supports ** recursive globs: e.g. '**/*.py', 'src/**/*.ts', '*.json'. "
                "Use this to discover files before reading them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern (e.g. **/*.py, src/**/*.ts)"},
                    "path":    {"type": "string", "description": "Root directory to search (default: current dir)"},
                    "limit":   {"type": "integer","description": "Max files to return (default 200)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notebook_read",
            "description": "Read a Jupyter notebook (.ipynb) — returns all cells with source and outputs as formatted text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the .ipynb file"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notebook_edit",
            "description": "Edit a specific cell in a Jupyter notebook by its index (0-based). Clears cell outputs after edit.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":        {"type": "string",  "description": "Path to the .ipynb file"},
                    "cell_index":  {"type": "integer", "description": "0-based cell index to replace"},
                    "new_source":  {"type": "string",  "description": "New cell source code/text"},
                },
                "required": ["path", "cell_index", "new_source"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_data",
            "description": (
                "Fetch real-time market data for any stock, ETF, index, or cryptocurrency. "
                "Returns price, change, high/low, volume, RSI(14), MACD histogram, MA20/60, "
                "Bollinger Bands. Supports: US tickers (AAPL, NVDA), A-shares (6-digit code like 600519), "
                "HK stocks (0700.HK), crypto (BTC, ETH), indices (SPY, QQQ). "
                "You must look up the correct ticker symbol yourself — e.g. LVMH → MC.PA, "
                "路易威登/路易斯威登 → MC.PA or LVMUY, 宝马 → BMW.DE, 大众 → VWAGY."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": (
                            "Ticker symbol. Examples: AAPL, NVDA, 600519, 0700.HK, BTC, MC.PA. "
                            "For A-shares use the 6-digit code without exchange suffix. "
                            "Do NOT guess — if unsure about a ticker, say so and ask the user."
                        ),
                    },
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_history",
            "description": (
                "Fetch OHLC price history for a stock, ETF, index, or cryptocurrency. "
                "Returns a COMPACT summary (period high/low, start/end close, % change, "
                "avg volume, MA5/MA20/MA60) plus the most recent ~30 candles — not the "
                "full series. Use this whenever you need historical prices, trend, or to "
                "compute your own indicators. A-shares (6-digit code) route through the "
                "user's configured Tushare first (if set), then Eastmoney/Sina/AKShare; "
                "HK (0700.HK) and US/global route through yfinance. "
                "IMPORTANT: prefer this tool over writing your own akshare/tushare/yfinance "
                "scripts — it handles source fallback and never depends on the local Python env."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Ticker, e.g. AAPL, 600519, 0700.HK, BTC. A-shares: 6-digit code, no suffix.",
                    },
                    "days": {
                        "type": "integer",
                        "description": "Lookback window in calendar days (default 120, max 1000).",
                    },
                    "interval": {
                        "type": "string",
                        "enum": ["1d", "1w", "1mo"],
                        "description": "Candle interval (default '1d'). Tushare path supports daily only.",
                    },
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "broker_query",
            "description": (
                "Query a connected brokerage account for account balance, current holdings (positions), "
                "or order history. Use this when the user asks about their portfolio, cash balance, "
                "unrealized P&L, or recent orders. This tool is READ-ONLY — it never places or cancels orders. "
                "Call with query='account' for cash/balance, query='positions' for holdings, "
                "query='orders' for order history."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "enum": ["account", "positions", "orders"],
                        "description": "What to query: 'account' = cash/balance, 'positions' = holdings, 'orders' = order list",
                    },
                    "broker_id": {
                        "type": "string",
                        "description": f"Optional broker id from {CONFIG_DIR}/brokers.json. Omit to use the active/default broker.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["all", "open", "filled", "cancelled"],
                        "description": "For orders query: filter by status. Default 'all'.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of orders to return (default 20).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "broker_order",
            "description": (
                "Propose a trade order (buy or sell). "
                "IMPORTANT: This tool requires explicit user confirmation. "
                "When called without confirmed=true, it returns an order preview with a "
                "preview_id and confirmation prompt. Only set confirmed=true after the user has explicitly "
                "said '确认下单', 'confirm order', or equivalent in this conversation turn. "
                "When confirmed=true, pass the exact preview_id from the prior preview. "
                "NEVER set confirmed=true on your own initiative."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Required for a new preview. Stock/ETF ticker symbol, e.g. AAPL, 600519, 0700.HK",
                    },
                    "side": {
                        "type": "string",
                        "enum": ["buy", "sell"],
                        "description": "Required for a new preview. Trade direction: 'buy' to purchase, 'sell' to liquidate",
                    },
                    "quantity": {
                        "type": "integer",
                        "description": "Required for a new preview. Number of shares/units to trade (positive integer)",
                    },
                    "price": {
                        "type": "number",
                        "description": "Limit price. Omit for market orders.",
                    },
                    "order_type": {
                        "type": "string",
                        "enum": ["limit", "market"],
                        "description": "Order type: 'limit' (default) or 'market'",
                    },
                    "confirmed": {
                        "type": "boolean",
                        "description": "Set to true ONLY after the user explicitly confirmed the order in this turn.",
                    },
                    "preview_id": {
                        "type": "string",
                        "description": "Required when confirmed=true. Use the preview_id returned by the prior broker_order preview.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lsp_hover",
            "description": "Get documentation or type signature for a symbol at a specific line and column using the Language Server Protocol (LSP).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative path to the file"},
                    "line": {"type": "integer", "description": "1-based line number"},
                    "col": {"type": "integer", "description": "1-based column number"}
                },
                "required": ["path", "line", "col"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "lsp_definition",
            "description": "Find the definition of a symbol at a specific line and column (LSP). Returns file path, line, and column.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative path to the file"},
                    "line": {"type": "integer", "description": "1-based line number"},
                    "col": {"type": "integer", "description": "1-based column number"}
                },
                "required": ["path", "line", "col"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "lsp_references",
            "description": "Find all references to a symbol at a specific line and column (LSP). Returns file paths, lines, and columns.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative path to the file"},
                    "line": {"type": "integer", "description": "1-based line number"},
                    "col": {"type": "integer", "description": "1-based column number"}
                },
                "required": ["path", "line", "col"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": "Pause execution and ask the user a clarifying question when requirements are ambiguous or a major design decision is needed. The workflow will halt until the user responds.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The question to ask the user. Be specific about what options or information you need."}
                },
                "required": ["question"]
            }
        }
    },
        __import__("aria_code.runtime.processes", fromlist=["PROCESS_SCHEMA"]).PROCESS_SCHEMA,
    ]
