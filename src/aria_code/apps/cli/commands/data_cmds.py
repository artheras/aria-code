"""DataCommandsMixin — data, alert, correlation, and comparison commands."""

from __future__ import annotations

from aria_code.apps.cli.i18n import ui_text

from ._ui import print_error


import json
import asyncio
import datetime
import time
import shlex
from typing import Dict, Any, Optional

def _render_portfolio_bt(*args, **kwargs):
    from aria_cli import _render_portfolio_bt as fn
    return fn(*args, **kwargs)
def _render_alerts(*args, **kwargs):
    from aria_cli import _render_alerts as fn
    return fn(*args, **kwargs)
def _get__HAS_LOCAL_FINANCE():
    from aria_cli import _HAS_LOCAL_FINANCE as val
    return val
def _get_LOCAL_TOOLS():
    # Owned by apps/cli/tool_registry.py; aria_cli fills it in place.
    from ..tool_registry import LOCAL_TOOLS as val
    return val
def _render_corr_matrix(*args, **kwargs):
    from aria_cli import _render_corr_matrix as fn
    return fn(*args, **kwargs)
def _render_peer_comparison(*args, **kwargs):
    from aria_cli import _render_peer_comparison as fn
    return fn(*args, **kwargs)
def _render_sql_result(*args, **kwargs):
    from aria_cli import _render_sql_result as fn
    return fn(*args, **kwargs)

import json
import asyncio
import datetime
import time
import shlex
import sys
import os
from typing import Dict, Any, Optional


import json
import asyncio
import datetime
import time
import shlex
import sys
import os
from typing import Dict, Any, Optional


class DataCommandsMixin:
    """Mixin: data analysis and comparison commands."""

    async def cmd_data(self, args: str):
        """/data（已迁移至 Arthera 后端服务）."""
        if self.context.has_rich:
            self.context.console.print("[dim]/data 已迁移至 Arthera 后端服务。[/dim]")

    async def cmd_alert(self, args: str):
        """/alert（已迁移至 Arthera 后端服务）."""
        if self.context.has_rich:
            self.context.console.print("[dim]/alert 已迁移至 Arthera 后端服务。[/dim]")

    async def cmd_corr(self, args: str):
        """/corr（已迁移至 Arthera 后端服务）."""
        if self.context.has_rich:
            self.context.console.print("[dim]/corr 已迁移至 Arthera 后端服务。[/dim]")

    async def cmd_portfolio_bt(self, args: str):
        """/portfolio-bt（已迁移至 Arthera 后端服务）."""
        if self.context.has_rich:
            self.context.console.print("[dim]/portfolio-bt 已迁移至 Arthera 后端服务。[/dim]")

    async def cmd_peer(self, args: str):
        """Peer comparison moved to Arthera backend / financial-service MCP."""
        if self.context.has_rich:
            self.context.console.print("[dim]/peer 已迁移至 Arthera 后端 / financial-service MCP。[/dim]")

    async def cmd_compare(self, args: str):
        """Strategy comparison moved to Arthera backend / financial-service MCP."""
        if self.context.has_rich:
            self.context.console.print("[dim]/compare 已迁移至 Arthera 后端 / financial-service MCP。[/dim]")
