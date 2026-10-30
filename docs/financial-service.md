# Shared Financial Service

The independently installable service lives at
https://github.com/artheras/financial-service. It exposes six read-only MCP tools:
service information, statement analysis, annual SEC facts parsing, risk metrics,
supplied-data backtests and optional historical market data. It does not import
Aria Code. The finance plugin in artheras/aria-plugins supplies workflow guidance
and host-specific configuration rather than another copy of the algorithms.

Install the service into a separate Python 3.11+ environment from its repository.
Install from source:

```sh
git clone https://github.com/artheras/financial-service.git
cd financial-service
python3 -m venv .venv
.venv/bin/python -m pip install -e .
# Optional Yahoo data support: .venv/bin/python -m pip install -e ".[market]"
```

Use that environment's absolute interpreter path in Aria Code:

```text
/mcp finance install "/absolute/path/to/financial-service/.venv/bin/python"
/mcp reload
/mcp tools
```

On Windows use the absolute .venv/Scripts/python.exe path, quoted if needed.
This command writes only MCP configuration, preserving other servers and unknown
host settings. It refuses malformed existing configuration and replaces a valid
file atomically. It does not install packages, authorize networking or run tools.
The MCP client runs through the Python application worker and supports the Rust frontend.

Financial tools appear with an mcp__arthera-finance__ prefix. A JSON configuration
template is also supplied by the finance plugin. The service starts offline;
supplied financial statements, SEC facts, risk calculations and backtests work
without external data. A user may explicitly edit the server's environment to set
FINANCIAL_SERVICE_NETWORK=1 for Yahoo/SEC reads, and EDGAR_USER_AGENT for SEC's
company/contact identification. Restart the server after changing host settings.
Tool arguments cannot enable networking.

