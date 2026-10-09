"""Semantic diff: which definitions a turn touched."""

import difflib
import json

from aria_code.runtime.delivery import DeliveryLedger, DeliveryReport
from aria_code.runtime.review import parse_review
from aria_code.runtime.semantic_diff import file_symbol_changes, reverse_apply, symbol_changes

V0 = (
    "import time\n\n\n"
    "class Session:\n"
    "    def refresh(self):\n"
    "        return fetch(self.token)\n\n"
    "    def close(self):\n"
    "        pass\n\n\n"
    "def legacy_refresh(s):\n"
    "    return s.refresh()\n\n\n"
    "def helper():\n"
    "    return 1\n"
)
V1 = V0.replace("        return fetch(self.token)", "        with self._lock:\n            return fetch(self.token)")
V2 = V1.replace("def legacy_refresh(s):\n    return s.refresh()\n\n\n", "") + (
    "\n\nclass SessionManager:\n    def get(self):\n        return None\n")


def _diff(a, b, name="s.py"):
    return "".join(difflib.unified_diff(a.splitlines(True), b.splitlines(True), f"a/{name}", f"b/{name}"))


def test_reverse_apply_recovers_each_earlier_version():
    assert reverse_apply(V2, _diff(V1, V2)) == V1
    assert reverse_apply(V1, _diff(V0, V1)) == V0


def test_a_diff_that_does_not_fit_gives_no_answer():
    assert reverse_apply("something else entirely\n", _diff(V0, V1)) is None


def test_added_modified_removed_across_two_edits(tmp_path):
    path = tmp_path / "s.py"
    path.write_text(V2)
    labels = [c.label() for c in file_symbol_changes(path, [_diff(V0, V1), _diff(V1, V2)])]
    assert labels == ["SessionManager added", "Session.refresh() modified", "legacy_refresh() removed"]


def test_methods_of_an_added_class_are_not_listed_separately():
    assert [c.label() for c in symbol_changes("", "class A:\n    def f(self):\n        pass\n", "python")] \
        == ["A added"]


def test_a_class_with_only_a_changed_method_reports_the_method():
    before = "class A:\n    x = 1\n\n    def f(self):\n        return 1\n"
    after = before.replace("return 1", "return 2")
    assert [c.label() for c in symbol_changes(before, after, "python")] == ["A.f() modified"]


def test_typescript():
    before = "export function load(a) {\n  return a\n}\n"
    after = before.replace("return a", "return a + 1") + "export function save(b) {\n  return b\n}\n"
    assert [c.label() for c in symbol_changes(before, after, "typescript")] == ["save() added", "load() modified"]


def test_unknown_language_or_a_file_changed_elsewhere_gives_nothing(tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_text("x\n")
    assert file_symbol_changes(notes, ["--- a\n+++ b\n@@ -1 +1 @@\n-y\n+x\n"]) is None
    # Changed by hand where the diff says what should be: no answer, not a wrong one.
    edited = tmp_path / "s.py"
    edited.write_text(V2.replace("class SessionManager:", "class SessionStore:"))
    assert file_symbol_changes(edited, [_diff(V1, V2)]) is None


def test_comments_moving_between_definitions_change_nothing():
    before = "def a():\n    return 1\n\n# about b\n"
    after = "def a():\n    return 1\n\n# about b\n\ndef b():\n    return 2\n"
    assert [c.label() for c in symbol_changes(before, after, "python")] == ["b() added"]


def test_the_delivery_report_lists_the_definitions(tmp_path):
    path = tmp_path / "s.py"
    path.write_text(V2)
    ledger = DeliveryLedger(root=str(tmp_path))
    for before, after in ((V0, V1), (V1, V2)):
        ledger.record("edit_file", {"path": str(path)},
                      {"success": True, "data": {"path": str(path), "applied": True, "diff": _diff(before, after)}})
    report = ledger.report()
    assert report.changed[0].symbols == ("SessionManager added", "Session.refresh() modified",
                                         "legacy_refresh() removed")
    text = DeliveryReport.from_dict(report.as_dict()).render(root=tmp_path)
    assert "      SessionManager added · Session.refresh() modified · legacy_refresh() removed" in text


def test_the_reviewer_describes_behaviour():
    answer = json.dumps({"verdict": "pass", "findings": [], "behaviour": {
        "before": "Concurrent 401s each refreshed the token.",
        "after": "They share one refresh.", "why": "Race on refresh", "impact": "AuthProvider"}})
    report = parse_review(answer)
    assert report.behaviour["after"] == "They share one refresh."
    rendered = DeliveryReport(status="done", review="x", behaviour=report.behaviour).render()
    assert "Behaviour" in rendered and "  After  They share one refresh." in rendered


def test_tested_by_names_the_tests_that_reference_what_changed(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    path = tmp_path / "src" / "session.py"
    path.write_text(V1)
    (tmp_path / "tests" / "test_session.py").write_text(
        "from src.session import Session\n\ndef test_refresh():\n    Session().refresh()\n")
    (tmp_path / "tests" / "test_other.py").write_text("def test_x():\n    thing.refresh()\n")
    (tmp_path / "src" / "uses.py").write_text("from src.session import Session\nSession().refresh()\n")
    ledger = DeliveryLedger(root=str(tmp_path))
    ledger.record("edit_file", {"path": str(path)},
                  {"success": True, "data": {"path": str(path), "applied": True, "diff": _diff(V0, V1)}})
    item = ledger.report().changed[0]
    assert item.symbols == ("Session.refresh() modified",)
    # test_other.py says "refresh" but never "Session"; uses.py is not a test.
    assert item.tested_by == ("tests/test_session.py",)
    assert "      tested by tests/test_session.py" in ledger.report().render()
