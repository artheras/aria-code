# Shared Financial Service

The independently installable service lives at
https://github.com/artheras/financial-service. It exposes ten read-only MCP tools:
service information, statement analysis, annual SEC facts parsing, risk metrics,
supplied-data backtests, optional historical market data and broker account queries. It does not import
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


Broker connections are configured per user in the Financial Service's private profile directory. Follow its [broker connection guide](https://github.com/artheras/financial-service/blob/main/docs/broker-connections.md).

Set the MCP server environment to FINANCIAL_SERVICE_BROKERS_ENABLED=1 and, for real broker connections, FINANCIAL_SERVICE_NETWORK=1. FINANCIAL_SERVICE_HOME selects that user's profile directory. Keep the service process and directory separate for each user; this local connection is not a hosted multi-tenant login service. Credentials are resolved by the service host from environment references, never passed in tool arguments.

Use /mcp reload after configuration, then ask the Finance tools to list connections and read the selected account, positions or existing orders. The tool arguments contain only a connection ID and query options. These tools cannot place, modify or cancel orders.
