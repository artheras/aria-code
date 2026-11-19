"""DataCommandsMixin — data, alert, correlation, and comparison command stubs."""

from __future__ import annotations


class DataCommandsMixin:
    """Mixin: migrated data analysis and comparison command stubs."""

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
