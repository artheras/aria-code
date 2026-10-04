import io
import unittest

from rich import box
from rich.console import Console

# Same module object the names below come from: `ui.robot` (via src/aria_code)
# and `aria_code.ui.robot` (via src) are distinct modules with separate
# _theme_cache globals, so setting it on one left get_robot_row reading the
# other and the light/dark assertions compared two identical palettes.
import aria_code.ui.robot as robot
from aria_code.ui.banner import render_full_banner
from aria_code.ui.robot import ROBOT_ROW_COUNT, RobotState, get_robot_row, get_status_dot, set_robot_state


class RobotBannerTests(unittest.TestCase):
    def setUp(self):
        robot._theme_cache = "dark"  # deterministic palette for assertions

    def tearDown(self):
        set_robot_state(RobotState.IDLE)
        robot._theme_cache = None

    def test_robot_preserves_reference_proportions_and_transparent_outline(self):
        rows = robot._art_rows(28)
        self.assertEqual(len(rows), 13)
        self.assertTrue(all(sum(len(text) for _, text in row) == 28 for row in rows))
        self.assertEqual(rows[0][0][0], "")
        # Black screen, cream shell and amber details come from RGB pixels.
        from rich.style import Style
        colours = [Style.parse(style).color.triplet for row in rows for style, _ in row if style]
        self.assertTrue(any(max(rgb) < 30 for rgb in colours))
        self.assertTrue(any(min(rgb) > 210 for rgb in colours))
        self.assertTrue(any(rgb[0] > 200 and 110 < rgb[1] < 205 and rgb[2] < 130 for rgb in colours))

    def test_robot_does_not_recolour_the_supplied_artwork(self):
        robot._theme_cache = "light"
        light = [get_robot_row(2, row) for row in range(ROBOT_ROW_COUNT)]
        robot._theme_cache = "dark"
        dark = [get_robot_row(2, row) for row in range(ROBOT_ROW_COUNT)]
        self.assertEqual(light, dark)

    def test_compact_robot_keeps_the_original_aspect_ratio(self):
        rows = robot._art_rows(20)
        self.assertEqual(len(rows), 9)
        self.assertTrue(all(sum(len(text) for _, text in row) == 20 for row in rows))

    def test_idle_status_dot_does_not_blink_to_dim_dot(self):
        set_robot_state(RobotState.IDLE)

        text = "".join(fragment for _, fragment in get_status_dot(0))

        self.assertEqual(text, "•")

    def test_full_banner_uses_pixel_robot_and_runtime_dashboard(self):
        console = Console(file=io.StringIO(), record=True, width=120, force_terminal=False)

        render_full_banner(
            version="4.1.0",
            rt_label="GPT-OSS 120B  cloud",
            cwd="~/Desktop/aria-code",
            control_status_rich="workspace-write · network on · privacy local-only",
            ollama_status_rich="Ollama online · 3 models",
            tool_count=71,
            skill_count=14,
            first_run=True,
            console=console,
            has_rich=True,
            rich_box=box,
            lang="en",
        )

        rendered = console.export_text()
        self.assertIn("~/Desktop/aria-code", rendered)
        self.assertIn("71 tools", rendered)
        self.assertIn("Quick start", rendered)
        self.assertIn("workspace-write", rendered)
        self.assertNotIn("┌──┐", rendered)
        self.assertNotIn("╔══════════════╗", rendered)
        self.assertIn("╭", rendered)


if __name__ == "__main__":
    unittest.main()
