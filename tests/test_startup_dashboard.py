import io
from contextlib import redirect_stdout

from rich import box
from rich.console import Console

import aria_code.ui.robot as robot
from aria_code.ui.banner import control_status_label, render_startup_dashboard
from aria_code.ui.startup_dashboard import StartupDashboardViewModel, select_dashboard_layout


def _view(**overrides):
    values = {
        "version": "4.1.1",
        "runtime_label": "GPT-OSS 120B  [dim]cloud[/dim]",
        "cwd": "~/Desktop/aria-code",
        "control_status": "workspace-write · network on · privacy local-only",
        "health_status": "Ollama online · 3 models",
        "tool_count": 71,
        "skill_count": 14,
        "lang": "en",
    }
    values.update(overrides)
    return StartupDashboardViewModel(**values)


def _render(width, **overrides):
    robot._theme_cache = "dark"
    stream = io.StringIO()
    console = Console(file=stream, record=True, width=width, force_terminal=False)
    render_startup_dashboard(
        _view(**overrides),
        console=console,
        has_rich=True,
        rich_box=box,
        terminal_width=width,
    )
    robot._theme_cache = None
    return console.export_text()


def test_layout_breakpoints_are_stable():
    assert select_dashboard_layout(55) == "minimal"
    assert select_dashboard_layout(70) == "stacked"
    assert select_dashboard_layout(70, height=24) == "minimal"
    assert select_dashboard_layout(80, height=24) == "minimal"
    assert select_dashboard_layout(80, height=40) == "stacked"
    assert select_dashboard_layout(120) == "wide"


def test_wide_first_run_uses_two_section_dashboard():
    rendered = _render(120, first_run=True)

    assert "Quick start" in rendered
    assert "Runtime" in rendered
    assert "71 tools" in rendered
    assert "workspace-write" in rendered
    assert "╭" in rendered


def test_80_column_short_terminal_keeps_model_and_runtime_visible():
    rendered = _render(80)

    assert "GPT-OSS 120B" in rendered
    assert "Quick start" not in rendered
    assert "Ollama online" in rendered
    assert "workspace-write" in rendered
    assert len(rendered.splitlines()) <= 11
    assert all(len(line) <= 80 for line in rendered.splitlines())


def test_80_column_first_run_does_not_wrap():
    rendered = _render(
        80,
        first_run=True,
        git_branch="main",
        git_dirty=True,
        mcp_server_count=1,
    )

    assert "Describe the task naturally" in rendered
    assert "MCP 1 · 71 tools" in rendered
    assert len(rendered.splitlines()) <= 11


def test_minimal_layout_drops_panel_chrome():
    rendered = _render(55)

    assert "Aria Code" in rendered
    assert "71 tools · 14 skills" in rendered
    assert "╭" not in rendered
    assert "Runtime" not in rendered
    assert "▀" in rendered


def test_narrow_terminal_keeps_update_reminder_visible():
    rendered = _render(55, update_notice="Update available: aria update")
    assert "Update available: aria update" in rendered


def test_plain_terminal_also_shows_robot():
    output = io.StringIO()
    with redirect_stdout(output):
        render_startup_dashboard(
            _view(), console=None, has_rich=False, rich_box=None,
        )
    assert "▀" in output.getvalue()


def test_chinese_view_model_localizes_sections():
    view = _view(lang="zh", first_run=True)

    assert view.getting_started_title == "快速开始"
    assert view.runtime_title == "运行状态"
    assert view.whats_new_title == "版本更新"
    assert view.capabilities == "71 个工具 · 14 个技能"


def test_capabilities_include_configured_mcp_servers():
    view = _view(mcp_server_count=2)

    assert view.capabilities == "MCP 2 · 71 tools · 14 skills"


def test_control_status_uses_retention_wording_and_localizes_permission():
    config = {
        "permission_mode": "workspace-write",
        "network_enabled": True,
        "data_sharing": False,
        "feedback_upload": False,
    }

    assert control_status_label(config, lang="en") == (
        "workspace-write · network on · local retention"
    )
    assert control_status_label(config, lang="zh") == (
        "工作区可写 · 网络开 · 本地留存"
    )
