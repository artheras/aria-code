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

    def test_robot_is_the_artwork_in_quadrant_pixels(self):
        """4 rows × 9 columns, like Claude Code's mascot: cap corners, square eye,
        2:1 dash, ear nubs, base and four feet — no image, no blended colours."""
        rows = ["".join(text for _, text in get_robot_row(2, row)) for row in range(ROBOT_ROW_COUNT)]

        self.assertEqual(rows, [
            "▗▛▀▀▀▀▀▜▖",
            "▌▌▗▖ ▗▖▐▐",
            "▐▙▄▄▄▄▄▟▌",
            "▝▀▀▀▀▀▀▀▘",
        ])

    def test_robot_uses_the_artworks_colours(self):
        # Sampled from docs/assets/aria-robot.png on the robot's 18×8 grid.
        styles = [style for row in range(ROBOT_ROW_COUNT) for style, _ in get_robot_row(2, row)]

        self.assertIn("#F4EBE4 on #0E0E0E", styles)   # cream shell around the black screen
        self.assertIn("#FFFDF5 on #0E0E0E", styles)   # square eye, brighter than the shell
        self.assertIn("#B6ADA4 on #F4EBE4", styles)   # grey ear nub on the shell
        self.assertIn("#B6ADA4 on #D5CCC3", styles)   # grey base over a light foot

    def test_robot_palette_follows_theme(self):
        robot._theme_cache = "light"
        light = [s for row in range(ROBOT_ROW_COUNT) for s, _ in get_robot_row(2, row)]
        robot._theme_cache = "dark"
        dark = [s for row in range(ROBOT_ROW_COUNT) for s, _ in get_robot_row(2, row)]

        self.assertNotEqual(light, dark)
        # The shell and greys are deepened on a light terminal, where the
        # artwork's would vanish; the screen and eyes stay as drawn.
        self.assertIn("#E6DDD0 on #0E0E0E", light)

    def test_idle_status_dot_does_not_blink_to_dim_dot(self):
        set_robot_state(RobotState.IDLE)

        text = "".join(fragment for _, fragment in get_status_dot(0))

        self.assertEqual(text, "•")

    def test_full_banner_is_the_robot_and_four_lines(self):
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

        lines = console.export_text().rstrip("\n").splitlines()
        self.assertEqual(lines[0].split()[1:4], ["Aria", "Code", "v4.1.0"])
        self.assertIn("~/Desktop/aria-code", lines[2])
        self.assertIn("71 tools", lines[3])
        self.assertIn("workspace-write", lines[3])
        self.assertIn("Describe the task naturally", lines[4])   # first-run note, under the robot
        self.assertEqual(len(lines), 5)
        self.assertFalse(any(ch in "".join(lines) for ch in "╭╰│"), "no frame")


if __name__ == "__main__":
    unittest.main()
