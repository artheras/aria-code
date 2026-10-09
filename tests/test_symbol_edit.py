"""edit_file by symbol: name the definition instead of quoting it."""

from aria_code.apps.cli.tools.write_tools import tool_edit_file
from aria_code.runtime.symbol_edit import definition_text, resolve_symbol_edit

SOURCE = (
    "import time\n\n\n"
    "class Session:\n"
    "    def refresh(self):\n"
    "        return fetch(self.token)\n\n"
    "    # Closing is idempotent.\n"
    "    def close(self):\n"
    "        pass\n\n\n"
    "def helper():\n"
    "    return 1\n"
)


def test_definition_text_covers_the_whole_definition_and_nothing_after():
    text, _ = definition_text(SOURCE, "python", "Session.refresh")
    assert text == "    def refresh(self):\n        return fetch(self.token)"
    text, _ = definition_text(SOURCE, "python", "Session")
    assert text.startswith("class Session:") and text.endswith("        pass") and "helper" not in text


def test_unknown_and_ambiguous_names_explain_themselves():
    assert "this file defines: Session, Session.refresh" in definition_text(SOURCE, "python", "nope")[1]
    twice = "class A:\n    def run(self):\n        pass\n\n\nclass B:\n    def run(self):\n        pass\n"
    why = definition_text(twice, "python", "run")[1]
    assert "names 2 definitions" in why and "A.run (line 2)" in why


def test_replace_a_method_written_flush_left(tmp_path):
    path = tmp_path / "s.py"
    path.write_text(SOURCE)
    params = {"path": str(path), "symbol": "Session.refresh",
              "new_string": "def refresh(self):\n    with self._lock:\n        return fetch(self.token)\n"}
    assert resolve_symbol_edit(params) is None
    assert params["old_string"].startswith("    def refresh")
    assert params["new_string"] == "    def refresh(self):\n        with self._lock:\n            return fetch(self.token)"
    assert resolve_symbol_edit(params) is None, "resolving twice changes nothing"


def test_insert_after(tmp_path):
    path = tmp_path / "s.py"
    path.write_text(SOURCE)
    result = tool_edit_file({"path": str(path), "symbol": "helper", "position": "after",
                             "new_string": "def other():\n    return 2\n", "_disable_checkpoints": True})
    assert result["success"], result
    assert path.read_text().endswith("def helper():\n    return 1\n\n\ndef other():\n    return 2\n")


def test_edit_file_reports_a_bad_symbol(tmp_path):
    path = tmp_path / "s.py"
    path.write_text(SOURCE)
    result = tool_edit_file({"path": str(path), "symbol": "Session.missing", "new_string": "x"})
    assert not result["success"] and result["error"].startswith("symbol edit: no definition named")


def test_plain_edits_are_untouched():
    params = {"path": "x.py", "old_string": "a", "new_string": "b", "symbol": "ignored"}
    assert resolve_symbol_edit(params) is None and params["old_string"] == "a"
