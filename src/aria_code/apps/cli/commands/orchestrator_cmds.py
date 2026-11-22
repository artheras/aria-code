"""User-visible task graph previews for complex work."""

from __future__ import annotations


class OrchestratorCommandsMixin:
    def cmd_orchestrate(self, args: str):
        """Preview the verifiable Agent task graph for a request."""
        from runtime.task_orchestrator import build_task_graph

        request = args.strip()
        if not request:
            message = "Usage: /orchestrate <任务描述>"
            self.context.console.print(f"[yellow]{message}[/yellow]") if self.context.has_rich else print(message)
            return
        graph = build_task_graph(request)
        if self.context.has_rich:
            from rich.table import Table

            table = Table(title=f"任务编排 · {graph.kind}", header_style="bold cyan")
            table.add_column("步骤", no_wrap=True)
            table.add_column("目标")
            table.add_column("依赖")
            table.add_column("验证门禁")
            for stage in graph.stages:
                table.add_row(
                    stage.title, stage.objective, "、".join(stage.depends_on) or "—",
                    stage.verification or "—",
                )
            self.context.console.print(table)
            ready = "、".join(stage.title for stage in graph.ready(()))
            self.context.console.print(f"[dim]可立即并行：{ready}。写入操作仍受权限与 worktree 隔离约束。[/dim]")
        else:
            for stage in graph.stages:
                deps = ", ".join(stage.depends_on) or "none"
                print(f"- {stage.title} [{deps}]: {stage.objective}")
