"""A release is started by a person and passes a checklist; merging alone never publishes."""

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release-on-merge.yml"


def _doc():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers(doc):
    return doc.get(True) or doc.get("on")


def test_a_merge_to_main_does_not_release():
    triggers = _triggers(_doc())
    assert "push" not in triggers and "pull_request" not in triggers
    assert set(triggers) == {"workflow_dispatch"}


def test_it_needs_a_typed_confirmation_and_offers_a_named_override():
    inputs = _triggers(_doc())["workflow_dispatch"]["inputs"]
    assert inputs["confirm"]["required"] is True
    assert inputs["skip_checks"]["default"] is False
    preflight = "\n".join(step.get("run", "") for step in _doc()["jobs"]["preflight"]["steps"])
    assert '"$CONFIRM" != "RELEASE"' in preflight


def test_nothing_is_tagged_before_the_checklist_passes():
    jobs = _doc()["jobs"]
    assert jobs["tag-next-patch"]["needs"] == "preflight"
    for later in ("binaries", "publish"):
        assert jobs[later]["needs"] == "tag-next-patch"
    assert "needs" not in jobs["preflight"]


def test_the_checklist_checks_main_and_that_something_changed():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert 'refs/heads/main' in text
    assert "Nothing has been merged since" in text
    assert "check-runs" in text and "skip_checks" in text


def test_the_changelog_lists_every_commit_since_the_last_release_not_one_merge():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "head_commit" not in text
    assert 'git log "$LAST_TAG..HEAD"' in text
