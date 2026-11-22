"""BrokerCommandsMixin — /broker /account /positions /orders commands.

Extracted from aria_cli.py. Module globals (self.context.has_rich, self.context.console, etc.) are
imported lazily inside each method body to avoid circular imports at load time.
"""

from __future__ import annotations

from ._ui import Panel, print_error, rich_box


class BrokerCommandsMixin:
    """Mixin providing broker/account/positions/orders commands."""

    async def cmd_broker(self, args: str):
        """Broker management moved to Arthera backend service."""
        if self.context.has_rich:
            self.context.console.print("[dim]/broker 已迁移至 Arthera 后端服务。[/dim]")

    async def _cmd_broker_add(self, broker_type: str = ""):
        """Broker setup moved to Arthera backend service."""
        if self.context.has_rich:
            self.context.console.print("[dim]/broker add 已迁移至 Arthera 后端服务。[/dim]")

    async def cmd_account(self, args: str):
        """Account summary moved to Arthera backend service."""
        if self.context.has_rich:
            self.context.console.print("[dim]/account 已迁移至 Arthera 后端服务。[/dim]")

    async def cmd_positions(self, args: str):
        """Positions query moved to Arthera backend service."""
        if self.context.has_rich:
            self.context.console.print("[dim]/positions 已迁移至 Arthera 后端服务。[/dim]")

    async def cmd_orders(self, args: str):
        """Orders query moved to Arthera backend service."""
        if self.context.has_rich:
            self.context.console.print("[dim]/orders 已迁移至 Arthera 后端服务。[/dim]")

    async def cmd_paper(self, args: str):
        """Paper trading moved to Arthera backend service."""
        if self.context.has_rich:
            self.context.console.print("[dim]/paper 已迁移至 Arthera 后端服务。[/dim]")

    async def cmd_trade(self, args: str):
        """两阶段交易: /trade mode | preview SYMBOL buy|sell QTY PRICE | confirm PREVIEW_ID | previews
        | allow-chat-confirm [broker_id] | disallow-chat-confirm [broker_id]."""
        from aria_code.apps.cli._optional import get_registry as _get_broker_registry
        from brokers import (
            OrderIntent, build_order_preview, execute_order_preview,
            list_order_previews,
        )
        from aria_code.safety import SafetyService

        parts = args.strip().split()
        sub = parts[0].lower() if parts else "mode"
        reg = _get_broker_registry()
        broker = reg.active() if reg else None
        if not broker:
            try:
                broker = reg.connect_default() if reg else None
            except Exception as exc:
                print_error(self.context, f"无法连接默认账户: {exc}", "先运行 /paper start 或 /broker connect <id>")
                return

        if sub == "mode":
            if not broker:
                print_error(self.context, "无活跃账户", "先运行 /paper start 或 /broker connect <id>")
                return
            # The broker's own config, not the terminal's — trading policy is
            # per-account (paper vs live lives on the account, not the session).
            policy = SafetyService(getattr(broker, "config", {}) or {}).trading_policy(
                getattr(broker, "broker_type", "")
            )
            msg = (
                f"账户: {broker.label} ({broker.broker_type})\n"
                f"模式: {policy.mode}\n"
                f"实盘允许: {policy.allow_live_trade}\n"
                f"确认要求: {policy.require_confirm}\n"
                f"单笔上限: {policy.max_order_value_weight:.1%}  单票仓位上限: {policy.max_single_position_weight:.1%}"
            )
            if self.context.has_rich:

                color = "red" if policy.mode == "live" else "green" if policy.mode == "paper" else "yellow"
                self.context.console.print(Panel(msg, title="[bold]Trade Mode[/bold]", border_style=color, box=rich_box.ROUNDED))
            else:
                print(msg)
            return

        if sub in ("allow-chat-confirm", "disallow-chat-confirm"):
            from brokers.config import get_broker_config, set_chat_confirm_enabled

            target_id = parts[1] if len(parts) > 1 else (broker.broker_id if broker else "")
            if not target_id:
                print_error(self.context, "请指定券商 id", "/trade allow-chat-confirm <broker_id>")
                return
            cfg = get_broker_config(target_id)
            if not cfg:
                print_error(self.context, f"未找到券商配置: {target_id}", "先用 /broker connect 配置好")
                return

            if sub == "disallow-chat-confirm":
                set_chat_confirm_enabled(target_id, False)
                msg = f"✓ 已关闭 {target_id} 的聊天内确认下单"
                self.context.console.print(f"[green]{msg}[/green]") if self.context.has_rich else print(msg)
                return

            # 这是唯一能打开这道门的地方——必须是真人坐在终端前，MCP/聊天
            # 没有任何路径能碰到这个函数。要求打完整的 broker_id，不是
            # 简单敲个 y，降低误触概率。
            warn = (
                f"这会允许通过 MCP/聊天(比如 Claude Code 连着这个项目的时候)"
                f"对 {target_id}({cfg.get('label', target_id)}) 发出的下单确认"
                f"真正执行下单，不再要求你本人在这个终端里手动敲 /trade confirm。\n\n"
                f"确认开启的话，完整输入券商 id「{target_id}」（不是 y/yes）："
            )
            self.context.console.print(f"[bold red]⚠ 高风险操作[/bold red]\n{warn}") if self.context.has_rich else print(warn)
            try:
                answer = self.context.console.input("> ") if self.context.has_rich else input("> ")
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer.strip() != target_id:
                msg = "未确认，聊天内确认下单保持关闭"
                self.context.console.print(f"[yellow]{msg}[/yellow]") if self.context.has_rich else print(msg)
                return
            set_chat_confirm_enabled(target_id, True)
            msg = f"✓ 已为 {target_id} 开启聊天内确认下单——之后 aria.broker.confirm_order 这个 MCP 工具能真正执行这个账户的订单了"
            self.context.console.print(f"[green]{msg}[/green]") if self.context.has_rich else print(msg)
            return

        if sub in ("previews", "list"):
            rows = list_order_previews(limit=10)
            if self.context.has_rich:
                from rich.table import Table

                tbl = Table(title="[bold]Trade Previews[/bold]", box=rich_box.ROUNDED, border_style="dim")
                tbl.add_column("ID")
                tbl.add_column("Mode")
                tbl.add_column("Broker")
                tbl.add_column("Order")
                tbl.add_column("Status")
                tbl.add_column("Can Exec")
                for row in rows:
                    intent = row.get("intent") or {}
                    tbl.add_row(
                        row.get("preview_id", ""),
                        row.get("mode", ""),
                        row.get("broker_label", ""),
                        f"{intent.get('side','')} {intent.get('symbol','')} x {intent.get('quantity','')}",
                        row.get("status", ""),
                        "yes" if row.get("can_execute") else "no",
                    )
                self.context.console.print(tbl)
            else:
                for row in rows:
                    print(row)
            return

        if sub == "confirm":
            if not broker:
                print_error(self.context, "无活跃账户", "先运行 /paper start 或 /broker connect <id>")
                return
            preview_id = parts[1] if len(parts) > 1 else ""
            if not preview_id:
                print_error(self.context, "请提供 preview_id", "/trade confirm <preview_id>")
                return
            result = execute_order_preview(broker, preview_id, confirmed=True)
            if result.get("success"):
                if self.context.has_rich:
                    self.context.console.print(
                        f"[green]✓[/green] 订单已执行 [{result.get('mode')}] "
                        f"{result.get('side')} {result.get('symbol')} x {result.get('qty')} "
                        f"[dim]#{result.get('order_id')}[/dim]"
                    )
                else:
                    print(result)
            else:
                print_error(self.context,
                    "订单未执行",
                    "; ".join(result.get("execution_blockers") or [result.get("error", "unknown")]),
                )
            return

        if sub == "preview":
            order_parts = parts[1:]
        else:
            order_parts = parts
        if len(order_parts) < 4:
            print_error(self.context, "用法: /trade preview SYMBOL buy|sell QTY PRICE", "/trade preview AAPL buy 10 190")
            return
        if not broker:
            print_error(self.context, "无活跃账户", "先运行 /paper start 或 /broker connect <id>")
            return
        symbol, side, qty_raw, price_raw = order_parts[:4]
        try:
            qty = float(qty_raw)
            price = float(price_raw)
        except Exception:
            print_error(self.context, "数量和价格必须是数字", "/trade preview AAPL buy 10 190")
            return
        if side.lower() not in ("buy", "sell"):
            print_error(self.context, "side 必须是 buy 或 sell", "/trade preview AAPL buy 10 190")
            return
        preview = build_order_preview(
            broker,
            OrderIntent(
                symbol=symbol,
                side=side,
                quantity=qty,
                price=price,
                order_type="limit",
                source="slash_trade",
            ),
        )
        blockers = preview.get("execution_blockers") or []
        if self.context.has_rich:

            status = "可执行" if preview.get("can_execute") else "不可执行"
            body = (
                f"preview_id: [bold]{preview.get('preview_id')}[/bold]\n"
                f"模式: {preview.get('mode')}  账户: {preview.get('broker_label')}\n"
                f"订单: {side.upper()} {symbol.upper()} x {qty:g} @ {price:g}\n"
                f"状态: {status}\n"
            )
            if blockers:
                body += "\n执行限制:\n" + "\n".join(f"  - {b}" for b in blockers)
            body += "\n\n确认执行: /trade confirm " + str(preview.get("preview_id"))
            self.context.console.print(Panel(body, title="[bold]Trade Preview[/bold]", border_style="yellow", box=rich_box.ROUNDED))
        else:
            print(preview)
