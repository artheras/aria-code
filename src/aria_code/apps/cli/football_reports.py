"""足球赛事报表渲染 —— 从 aria_cli.py 抽出的第一块。

这四个函数原本在 aria_cli.py 末尾（8512-8715 行）。它们几乎完全自包含：
rich 组件和 football_data_client 都是函数内 import，唯一的外部依赖是模块级
的 ``console``。

⚠️ 调用方式的约束（拆分本文件时最容易踩坏的地方）：
``apps/cli/commands/market_cmds.py`` 是一个 mixin，它用**裸名字**调用
``_football_standings(...)`` —— 这只有在 aria_cli 通过 ``_rebind_mixin_globals``
把 mixin 方法的 ``__globals__`` 指向 aria_cli 命名空间时才成立。所以本模块
**不能**只是被 import，必须由 aria_cli 用 ``_rebind_module_function_globals``
把这些函数重新绑定回它自己的 globals，``console`` 和裸名调用才都能解析。
这跟 aria_cli 对 ``apps.cli.tool_executor`` 的处理是同一套既有机制。

直接 ``from apps.cli.football_reports import _football_standings`` 再调用会
在运行期 NameError（找不到 console）——那是预期行为，不是缺陷：状态注入还
没做到每个调用点，兼容边界仍留在 aria_cli。
"""

from __future__ import annotations

__all__ = [
    "_football_standings",
    "_football_fixtures",
    "_football_team",
    "_football_h2h",
]


def _football_standings(league: str) -> None:
    from rich.table import Table
    from rich import box as rich_box
    from rich.panel import Panel
    try:
        from aria_code.clients.football_data_client import get_standings, LEAGUE_NAMES, _resolve_league
    except ImportError:
        console.print("[red]football_data_client.py 未找到[/red]")
        return

    console.print(f"[dim]获取 {league.upper()} 积分榜…[/dim]")
    data = get_standings(league)
    if not data:
        comp = _resolve_league(league)
        console.print(
            "[yellow]无法获取数据。请设置 FOOTBALL_DATA_API_KEY:[/yellow]\n"
            "  1. 访问 football-data.org 免费注册\n"
            "  2. 在 ~/.aria/.env 中添加:\n"
            "     [cyan]FOOTBALL_DATA_API_KEY=your_key_here[/cyan]"
        )
        return

    t = Table(
        title=f"[bold]{data['league_name']}[/bold]  {data['season_start'][:4]}/{data['season_end'][:4]}",
        box=rich_box.SIMPLE,
        show_header=True,
        header_style="bold",
        padding=(0, 1),
    )
    t.add_column("#",    width=3,  justify="right")
    t.add_column("球队", width=22)
    t.add_column("场",   width=4,  justify="right")
    t.add_column("胜",   width=3,  justify="right", style="green")
    t.add_column("平",   width=3,  justify="right", style="yellow")
    t.add_column("负",   width=3,  justify="right", style="red")
    t.add_column("进/失", width=7, justify="right")
    t.add_column("净胜", width=5,  justify="right")
    t.add_column("积分", width=5,  justify="right", style="bold cyan")
    t.add_column("近5场", width=7)

    for row in data["table"]:
        gd = row["gd"]
        gd_str = f"+{gd}" if gd > 0 else str(gd)
        form_colored = ""
        for c in (row.get("form") or ""):
            if c == "W":
                form_colored += "[green]W[/green]"
            elif c == "L":
                form_colored += "[red]L[/red]"
            elif c == "D":
                form_colored += "[yellow]D[/yellow]"
            else:
                form_colored += c
        t.add_row(
            str(row["pos"]),
            row["team"],
            str(row["played"]),
            str(row["w"]),
            str(row["d"]),
            str(row["l"]),
            f"{row['gf']}/{row['ga']}",
            gd_str,
            str(row["pts"]),
            form_colored,
        )

    console.print(t)


def _football_fixtures(league: str, days: int = 7) -> None:
    from rich.table import Table
    from rich import box as rich_box
    try:
        from aria_code.clients.football_data_client import get_fixtures, LEAGUE_NAMES, _resolve_league
    except ImportError:
        console.print("[red]football_data_client.py 未找到[/red]")
        return

    comp = _resolve_league(league)
    league_label = LEAGUE_NAMES.get(comp, comp)
    console.print(f"[dim]获取 {league_label} 未来 {days} 天赛程…[/dim]")

    matches = get_fixtures(league, days)
    if matches is None:
        console.print("[yellow]无法获取数据。请检查 FOOTBALL_DATA_API_KEY 设置。[/yellow]")
        return
    if not matches:
        console.print(f"[dim]未来 {days} 天内暂无赛事[/dim]")
        return

    t = Table(
        title=f"[bold]{league_label}[/bold]  未来 {days} 天赛程",
        box=rich_box.SIMPLE,
        show_header=True,
        header_style="bold",
        padding=(0, 1),
    )
    t.add_column("日期(UTC)", width=11)
    t.add_column("主队",      width=22)
    t.add_column("",          width=3,  justify="center")
    t.add_column("客队",      width=22)
    t.add_column("轮次",      width=5,  justify="right")

    for m in matches:
        t.add_row(
            m["date"],
            m["home"],
            "vs",
            m["away"],
            str(m["matchday"] or m.get("stage", "")),
        )

    console.print(t)


def _football_team(team: str, league: str = "pl") -> None:
    from rich.table import Table
    from rich import box as rich_box
    from rich.panel import Panel
    try:
        from aria_code.clients.football_data_client import get_team_stats
    except ImportError:
        console.print("[red]football_data_client.py 未找到[/red]")
        return

    console.print(f"[dim]获取 {team} 近期数据 ({league.upper()})…[/dim]")
    stats = get_team_stats(league, team)
    if not stats:
        console.print("[yellow]无法获取球队数据。请检查球队名称和联赛代码。[/yellow]")
        return

    form_colored = ""
    for c in stats["form"]:
        if c == "W":   form_colored += "[bold green]W[/bold green]"
        elif c == "L": form_colored += "[bold red]L[/bold red]"
        elif c == "D": form_colored += "[bold yellow]D[/bold yellow]"

    summary = (
        f"[bold]{stats['team']}[/bold]  |  近 {stats['last_n']} 场\n\n"
        f"  战绩:    {stats['w']}胜  {stats['d']}平  {stats['l']}负\n"
        f"  进球:    {stats['gf']}球  (场均 {stats['avg_gf']})\n"
        f"  失球:    {stats['ga']}球  (场均 {stats['avg_ga']})\n"
        f"  主场进球: 场均 {stats['home_avg_gf']}\n"
        f"  客场进球: 场均 {stats['away_avg_gf']}\n"
        f"  近5场:   {form_colored}"
    )
    console.print(Panel(summary, title="[bold green]⚽ 球队状态[/bold green]", border_style="green"))

    t = Table(box=rich_box.SIMPLE, show_header=True, padding=(0, 1))
    t.add_column("日期",     width=10)
    t.add_column("主队",     width=20)
    t.add_column("比分",     width=7, justify="center", style="bold")
    t.add_column("客队",     width=20)
    t.add_column("",         width=3)

    for r in stats["recent"]:
        result_style = {"W": "green", "D": "yellow", "L": "red"}.get(r["result"], "dim")
        t.add_row(
            r["date"],
            r["home"],
            r["score"],
            r["away"],
            f"[{result_style}]{r['result']}[/{result_style}]",
        )
    console.print(t)


def _football_h2h(t1: str, t2: str, league: str = "pl") -> None:
    from rich.table import Table
    from rich import box as rich_box
    from rich.panel import Panel
    try:
        from aria_code.clients.football_data_client import get_head_to_head
    except ImportError:
        console.print("[red]football_data_client.py 未找到[/red]")
        return

    console.print(f"[dim]获取 {t1} vs {t2} 历史对决 ({league.upper()})…[/dim]")
    data = get_head_to_head(t1, t2, league)
    if not data:
        console.print("[yellow]未找到历史对决记录。[/yellow]")
        return

    summary = (
        f"[bold]{data['team1']}[/bold] vs [bold]{data['team2']}[/bold]  |  共 {data['total']} 场\n\n"
        f"  {data['team1']} 胜: [green]{data['team1_wins']}[/green]\n"
        f"  平局:         [yellow]{data['draws']}[/yellow]\n"
        f"  {data['team2']} 胜: [red]{data['team2_wins']}[/red]"
    )
    console.print(Panel(summary, title="[bold]⚽ 历史交锋[/bold]", border_style="dim"))

    t = Table(box=rich_box.SIMPLE, show_header=True, padding=(0, 1))
    t.add_column("日期", width=10)
    t.add_column("主队", width=22)
    t.add_column("比分", width=7, justify="center", style="bold cyan")
    t.add_column("客队", width=22)

    for m in data["matches"]:
        t.add_row(m["date"], m["home"], m["score"], m["away"])
    console.print(t)


