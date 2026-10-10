"""Single source of truth for the package version.

__version__ used to live in aria_cli.py, which meant anything wanting the
version string had to import the whole interactive CLI — prompt_toolkit,
bootstrap hooks, the tool registry, all of it. The three consumers each worked
around that differently: crash_report and a diagnostics accessor imported
aria_cli anyway, and the MCP server went to importlib.metadata instead, with a
comment explaining that pulling in aria_cli was too heavy for a binary whose
job is speaking JSON-RPC over stdio.

Keeping it here costs nothing to import and gives all three the same answer.
Note that importlib.metadata reports what was *installed*, which is not the
same thing when running from a source checkout.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.128.0"
