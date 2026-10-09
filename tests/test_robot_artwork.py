import base64
import hashlib
import io
from pathlib import Path

import pytest
from rich.console import Console

from aria_code.ui import banner, image_render, robot_pixels


ASSET = Path(banner.__file__).parent / "assets" / "aria-robot.png"


def test_fallback_pixels_are_compiled_from_the_actual_reference():
    assert hashlib.sha256(ASSET.read_bytes()).hexdigest() == robot_pixels.SOURCE_SHA256
    face = banner._artwork_pixels()
    rows = face.plain.splitlines()
    assert len(rows) == robot_pixels.HEIGHT // 2
    assert all(len(row) == robot_pixels.WIDTH for row in rows)
    assert len(face.spans) == robot_pixels.WIDTH * robot_pixels.HEIGHT // 2


@pytest.mark.parametrize("method", ["iterm", "kitty"])
def test_native_graphics_contains_the_original_pixels_and_dimensions(method):
    from PIL import Image
    sequence = image_render.render_image(str(ASSET), 20, method, crop=robot_pixels.BOUNDS, cells_high=10)
    assert sequence
    if method == "iterm":
        assert "width=20;height=10;" in sequence
        encoded = sequence.split(":", 1)[1].removesuffix("\x07")
    else:
        assert "c=20," in sequence and "r=10,C=1" in sequence
        encoded = "".join(chunk.split(";", 1)[1] for chunk in sequence.split("\x1b_G")[1:])
        encoded = encoded.replace("\x1b\\", "")
    with Image.open(io.BytesIO(base64.b64decode(encoded))) as rendered, Image.open(ASSET) as source:
        expected = source.crop(robot_pixels.BOUNDS).convert("RGB")
        assert rendered.size == expected.size
        assert rendered.tobytes() == expected.tobytes()


def test_redirected_output_never_emits_inline_image_escapes(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    monkeypatch.delenv("ARIA_ROBOT_RENDER", raising=False)
    console = Console(file=io.StringIO(), force_terminal=True, width=100)
    _, _, rows, sequence = banner._mascot(console, 100)
    assert rows == 4 and sequence is None


def test_explicit_pixels_work_without_a_tty_and_narrow_layout_remains_compact(monkeypatch):
    monkeypatch.setenv("ARIA_ROBOT_RENDER", "pixels")
    console = Console(file=io.StringIO(), width=100, force_terminal=True, no_color=False, color_system="truecolor")
    face, cols, rows, sequence = banner._mascot(console, 100)
    assert cols == 20 and rows == 9 and sequence is None
    assert len(face.plain.splitlines()) == 9
    assert banner._mascot(console, 40)[1:3] == (9, 4)


def test_no_color_uses_silhouette_instead_of_a_solid_pixel_rectangle(monkeypatch):
    monkeypatch.setenv("ARIA_ROBOT_RENDER", "pixels")
    console = Console(file=io.StringIO(), force_terminal=True, no_color=True)
    assert banner._mascot(console, 100)[1:3] == (9, 4)


def test_tmux_uses_pixels_instead_of_unsupported_graphics(monkeypatch):
    monkeypatch.setenv("TMUX", "pane")
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    assert image_render.best_method() not in ("iterm", "kitty")


def test_native_banner_reserves_cells_and_restores_cursor_before_notes(monkeypatch):
    from aria_code.ui.startup_dashboard import StartupDashboardViewModel
    class Terminal(io.StringIO):
        def isatty(self):
            return True
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    monkeypatch.setenv("ARIA_ROBOT_RENDER", "auto")
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("STY", raising=False)
    stream = Terminal()
    console = Console(file=stream, width=100, force_terminal=True, color_system="truecolor", no_color=False)
    view = StartupDashboardViewModel(version="1", runtime_label="Google Cloud", cwd="~/project",
                                     control_status="workspace-write · network on", health_status="ready",
                                     tool_count=90, skill_count=14, update_notice="Update available: aria update")
    banner.render_startup_dashboard(view, console=console, has_rich=True)
    output = stream.getvalue()
    reserve, graphic = output.split("\x1b7\x1b[10A\r", 1)
    assert reserve.count("\n") == 10
    assert graphic.startswith("\x1b]1337;")
    assert graphic.index("\x1b8") < graphic.index("Update available")


def test_pyinstaller_hook_collects_artwork_for_both_import_roots(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("collect_args", Path(__file__).parents[1] / "scripts/pyinstaller_collect_args.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    namespace = {}
    exec((module.write_hook(tmp_path) / "hook-aria_code.py").read_text(), namespace)
    assert {dest for _, dest in namespace["datas"]} == {"aria_code/ui/assets", "ui/assets"}
    assert all(Path(source).read_bytes() == ASSET.read_bytes() for source, _ in namespace["datas"])
