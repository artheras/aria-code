"""Packaging constraints that a passing test suite cannot otherwise notice.

requires-python's upper bound is load-bearing: pandas_ta pulls numba, which
hard-refuses to build on 3.14, so without the bound pip starts the install and
dies deep in a numba source build with an error naming neither aria-code nor
pandas_ta. With it, pip refuses up front and says why.

It was added deliberately (1b2ca47) and silently removed by a CI commit about
ruff and pytest paths (4b46f32), leaving seven lines of comment explaining a
constraint the line below no longer expressed. Nothing failed, because no test
installs the package on 3.14.
"""

from __future__ import annotations

import pathlib
import re
import unittest

PYPROJECT = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"


def _requires_python() -> str:
    for line in PYPROJECT.read_text(encoding="utf-8").splitlines():
        if line.startswith("requires-python"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise AssertionError("pyproject.toml declares no requires-python")


class RequiresPythonKeepsItsUpperBound(unittest.TestCase):
    def test_an_upper_bound_is_declared(self):
        spec = _requires_python()
        self.assertRegex(
            spec, r"<\s*3\.\d+",
            "requires-python has no upper bound. numba refuses to build on "
            "3.14, so pip would start the install and fail deep in a source "
            "build instead of refusing up front.",
        )

    def test_lower_bound_still_admits_the_supported_range(self):
        self.assertRegex(_requires_python(), r">=\s*3\.10")

    def test_the_comment_and_the_constraint_agree(self):
        """The comment names a version; the constraint must use that one.

        The regression left an explanatory comment above a line that had
        stopped matching it, which is worse than no comment — it is why the
        drop went unnoticed.
        """
        text = PYPROJECT.read_text(encoding="utf-8")
        head = text[: text.index("requires-python")]
        commented = set(re.findall(r"3\.14", head.rsplit("\n\n", 1)[-1]))
        self.assertTrue(commented, "the rationale comment no longer names 3.14")
        upper = re.search(r"<\s*(3\.\d+)", _requires_python())
        self.assertIsNotNone(upper)
        self.assertIn(upper.group(1), commented,
                      "the upper bound is not the version the comment explains")

    def test_publish_workflow_pins_supported_python_for_wheel_build(self):
        """uv build without a pinned Python grabs 3.14+ and fails against <3.14."""
        publish_yml = (
            PYPROJECT.parent / ".github" / "workflows" / "publish.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "uv build --python 3.12",
            publish_yml,
            "publish.yml platform-wheels must pin --python 3.12 so uv does not "
            "default to Python 3.14+ and reject requires-python <3.14",
        )


if __name__ == "__main__":
    unittest.main()
