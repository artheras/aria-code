"""Tests for publish_artifact and the canvas sandbox.

Two things are guarded here. That the model can put something on the canvas
without a human wiring it up — the capability. And that what it puts there
cannot reach anything — the reason the capability is safe to have.
"""

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from aria_code.tools.artifact_tools import (
    ARTIFACT_TOOL_SCHEMAS,
    ARTIFACT_TOOLS,
    register_artifact_tools,
    tool_publish_artifact,
)


class PublishArtifactTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self._prev_cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.chdir, self._prev_cwd)

        # Keep generated artifacts inside the temp dir instead of the user's
        # real output folder.
        self._out = self.root / "out"
        self._prev_home = os.environ.get("ARIA_OUTPUT_ROOT")
        os.environ["ARIA_OUTPUT_ROOT"] = str(self._out)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._prev_home is None:
            os.environ.pop("ARIA_OUTPUT_ROOT", None)
        else:
            os.environ["ARIA_OUTPUT_ROOT"] = self._prev_home

    def test_publishing_inline_content_writes_a_rendered_file(self):
        result = tool_publish_artifact({
            "content": "<h1>Revenue</h1>",
            "filename": "revenue.html",
            "title": "Revenue Board",
        })
        self.assertTrue(result["success"], result.get("error"))
        path = Path(result["data"]["path"])
        self.assertTrue(path.is_file())
        self.assertIn("Revenue", path.read_text(encoding="utf-8"))

    def test_republishing_the_same_topic_stays_one_artifact(self):
        # This is what makes "update the dashboard" behave the way a person
        # expects: a new version, not a second artifact.
        first = tool_publish_artifact({"content": "<p>v1</p>", "filename": "d.html", "title": "Board"})
        second = tool_publish_artifact({"content": "<p>v2</p>", "filename": "d.html", "title": "Board"})
        self.assertEqual(first["data"]["topic"], second["data"]["topic"])
        self.assertNotEqual(first["data"]["path"], second["data"]["path"])

    def test_topic_defaults_to_the_title(self):
        result = tool_publish_artifact({"content": "<p>x</p>", "filename": "a.html", "title": "Q3 Report"})
        self.assertEqual(result["data"]["topic"], "Q3 Report")

    def test_publishing_an_existing_workspace_file(self):
        source = self.root / "report.html"
        source.write_text("<p>from disk</p>", encoding="utf-8")
        result = tool_publish_artifact({"path": "report.html"})
        self.assertTrue(result["success"], result.get("error"))
        self.assertIn("from disk", Path(result["data"]["path"]).read_text(encoding="utf-8"))

    def test_a_file_outside_the_workspace_is_refused(self):
        # Publishing renders a file in a browser; this is the one place the
        # tool has to say no.
        outside = Path(self._prev_cwd) / "definitely-not-published.html"
        result = tool_publish_artifact({"path": str(outside.resolve())})
        self.assertFalse(result["success"])

    def test_absolute_path_traversal_is_refused(self):
        result = tool_publish_artifact({"path": "/etc/hosts"})
        self.assertFalse(result["success"])
        self.assertIn("outside the workspace", result["error"])

    def test_a_filename_containing_a_path_is_refused(self):
        result = tool_publish_artifact({"content": "x", "filename": "../escape.html"})
        self.assertFalse(result["success"])
        self.assertIn("bare file name", result["error"])

    def test_an_unrenderable_type_is_refused(self):
        result = tool_publish_artifact({"content": "x", "filename": "tool.exe"})
        self.assertFalse(result["success"])
        self.assertIn("cannot be rendered", result["error"])

    def test_a_missing_file_is_reported_not_raised(self):
        result = tool_publish_artifact({"path": "nope.html"})
        self.assertFalse(result["success"])
        self.assertIn("No such file", result["error"])

    def test_neither_path_nor_content_is_reported(self):
        self.assertFalse(tool_publish_artifact({})["success"])

    def test_oversized_content_is_refused(self):
        result = tool_publish_artifact({"content": "x" * (9 * 1024 * 1024), "filename": "big.html"})
        self.assertFalse(result["success"])
        self.assertIn("exceeds", result["error"])

    def test_result_says_whether_a_canvas_is_open(self):
        result = tool_publish_artifact({"content": "<p>x</p>", "filename": "a.html"})
        self.assertIn("live", result["data"])
        self.assertIn("/canvas", result["data"]["hint"])


class ToolRegistrationTests(unittest.TestCase):
    def test_registration_adds_the_tool_and_its_schema(self):
        tools, schemas = {}, []
        count = register_artifact_tools(tools, schemas)
        self.assertEqual(count, 1)
        self.assertIn("publish_artifact", tools)
        self.assertEqual(schemas[0]["name"], "publish_artifact")

    def test_the_schema_tells_the_model_the_page_must_be_self_contained(self):
        # Without this the model emits <script src="https://cdn…">, the CSP
        # blocks it, and the artifact renders as a blank page.
        description = ARTIFACT_TOOL_SCHEMAS[0]["description"]
        self.assertIn("self-contained", description)
        self.assertIn("data:", description)

    def test_the_tool_is_registered_in_the_cli(self):
        import aria_code.aria_cli as cli

        self.assertIn("publish_artifact", cli.LOCAL_TOOLS)
        names = {s.get("function", {}).get("name") for s in cli.LOCAL_TOOL_SCHEMAS}
        self.assertIn("publish_artifact", names)

    def test_tools_map_shape_matches_the_registry_contract(self):
        handler, description = ARTIFACT_TOOLS["publish_artifact"]
        self.assertTrue(callable(handler))
        self.assertTrue(description)


class CanvasSandboxTests(unittest.TestCase):
    """The framed artifact in the native Rust Canvas server must not be able to reach its host."""

    def _source(self) -> str:
        canvas_rs = Path(__file__).resolve().parents[1] / "rust" / "cli" / "src" / "engine" / "canvas.rs"
        return canvas_rs.read_text(encoding="utf-8")

    def test_the_iframe_is_not_given_back_its_origin(self):
        # allow-scripts together with allow-same-origin is not additive — it
        # cancels the sandbox, and the framed document runs in the host page's
        # origin, where /api/artifacts is reachable.
        import re

        source = self._source()
        attributes = re.findall(r'sandbox="([^"]*)"', source)
        self.assertTrue(attributes, "no sandboxed iframe found in the canvas shell")
        for value in attributes:
            with self.subTest(sandbox=value):
                self.assertIn("allow-scripts", value)
                self.assertNotIn("allow-same-origin", value)

    def test_artifacts_are_served_with_a_content_security_policy(self):
        source = self._source()
        self.assertIn("default-src 'none'", source)
        self.assertIn("form-action 'none'", source)
        self.assertIn("base-uri 'none'", source)

    def test_the_policy_blocks_the_page_from_calling_out(self):
        self.assertIn("connect-src 'none'", self._source())

    def test_fonts_are_the_only_permitted_remote_origin(self):
        import re

        source = self._source()
        match = re.search(r'pub const ARTIFACT_CSP: &str = "([^"]+)";', source)
        self.assertIsNotNone(match, "ARTIFACT_CSP constant not found in canvas.rs")
        policy = match.group(1)
        remote = {
            token for token in policy.replace(";", " ").split()
            if token.startswith("https://")
        }
        self.assertEqual(remote, {"https://fonts.googleapis.com", "https://fonts.gstatic.com"})

    def test_responses_are_not_sniffed_or_cached(self):
        source = self._source()
        self.assertIn('("X-Content-Type-Options", "nosniff")', source)
        self.assertIn('("Cache-Control", "no-store")', source)

    def test_the_server_still_binds_only_to_loopback(self):
        self.assertIn('pub const DEFAULT_HOST: &str = "127.0.0.1";', self._source())


if __name__ == "__main__":
    unittest.main()
