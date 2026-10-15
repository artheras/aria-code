"""Planning a video with nothing to cut together.

With no assets and generation not allowed, the planner was still asked to
write an edit plan: it picked shots from an empty list, produced beats whose
asset was "", and failed validation a revision later. That is roughly a
minute of paid model calls to reach a conclusion that is available before the
first one — and the error it ended on ("Beat beat_1 uses asset '', which is
not among the provided files") describes the model's output rather than the
thing the person has to fix.
"""
from __future__ import annotations

import pytest

from aria_code.video_workflow.pipeline import VideoWorkflowError, plan_video


def test_no_material_is_refused_before_any_model_call(tmp_path, monkeypatch):
    def fail(*_a, **_k):  # pragma: no cover - the point is that it is not called
        raise AssertionError("the planner was called with nothing to plan from")

    monkeypatch.setattr("aria_code.video_workflow.pipeline.understand_assets", fail)
    monkeypatch.setattr("aria_code.video_workflow.pipeline.write_edit_plan", fail)

    with pytest.raises(VideoWorkflowError, match="No material"):
        plan_video("做个视频", [], str(tmp_path / "proj"))


def test_the_refusal_says_both_ways_out(tmp_path):
    with pytest.raises(VideoWorkflowError) as excinfo:
        plan_video("做个视频", [], str(tmp_path / "proj"))
    message = str(excinfo.value)
    assert "attach" in message and "generated" in message


def test_generation_allowed_is_not_refused(tmp_path, monkeypatch):
    # Generating every shot is a real way to make a video from a script alone,
    # so the guard must not stand in front of it.
    called: list[str] = []
    monkeypatch.setattr("aria_code.video_workflow.pipeline.understand_assets", lambda *a, **k: [])
    monkeypatch.setattr(
        "aria_code.video_workflow.pipeline.write_edit_plan",
        lambda *a, **k: called.append("planned") or _stub_plan(),
    )
    plan_video("做个视频", [], str(tmp_path / "proj"), allow_generation=True)
    assert called == ["planned"]


def _stub_plan():
    class _Plan:
        beats: list = []

        def to_dict(self):
            return {"beats": []}

        def generated_seconds(self):
            return 0

    return _Plan()
