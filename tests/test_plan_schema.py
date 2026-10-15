"""The plan schema states the rules, instead of only asking for them.

A plan made from one attached image failed with "Beat b1 uses asset '',
which is not among the provided files" on every beat, and "Assets A1 are not
used although the video has room" — after two full rounds of paid model
calls, with the image sitting there unused.

The model was not ignoring the material. It answered source="asset" for every
beat and simply left asset_id out: the field was optional in the response
schema, structured output drops optional fields, and the rule that an asset
shot must name its asset existed only in the prose instructions.
"""
from __future__ import annotations

from aria_code.video_workflow.pipeline import _plan_schema
from aria_code.video_workflow.plan import Asset


def asset(asset_id: str) -> Asset:
    return Asset(id=asset_id, path=f"/tmp/{asset_id}.png", kind="image")


def beat_of(schema):
    return schema["properties"]["beats"]["items"]


def test_an_asset_shot_must_name_its_asset_when_nothing_may_be_generated():
    beat = beat_of(_plan_schema([asset("A1")], allow_generation=False))
    assert "asset_id" in beat["required"]


def test_only_the_ids_that_exist_can_be_chosen():
    # Stops the other half of the failure too: an id the model invented.
    beat = beat_of(_plan_schema([asset("A1"), asset("A2")], allow_generation=False))
    assert beat["properties"]["asset_id"]["enum"] == ["A1", "A2"]


def test_with_generation_off_there_is_no_other_source_to_pick():
    # Both remaining sources are paid, so "asset" is the only legal answer.
    beat = beat_of(_plan_schema([asset("A1")], allow_generation=False))
    assert beat["properties"]["source"]["enum"] == ["asset"]


def test_generation_allowed_leaves_the_sources_open():
    beat = beat_of(_plan_schema([asset("A1")], allow_generation=True))
    assert set(beat["properties"]["source"]["enum"]) >= {"asset", "generate", "animate"}


def test_a_generated_shot_is_not_forced_to_name_an_asset():
    # It has none; requiring one would break the case this does not cover.
    beat = beat_of(_plan_schema([asset("A1")], allow_generation=True))
    assert "asset_id" not in beat["required"]
    assert beat["properties"]["asset_id"]["enum"] == ["A1"]


def test_with_no_assets_nothing_is_constrained_to_an_empty_list():
    # An empty enum would make the schema unsatisfiable rather than permissive.
    beat = beat_of(_plan_schema([], allow_generation=True))
    assert "enum" not in beat["properties"]["asset_id"]
    assert "asset_id" not in beat["required"]


def test_the_shared_schema_is_not_mutated_between_requests():
    # It is module state; narrowing it in place would leak one request's
    # assets into the next.
    from aria_code.video_workflow.pipeline import _PLAN_SCHEMA

    before = _PLAN_SCHEMA["properties"]["beats"]["items"]["properties"]["asset_id"].copy()
    _plan_schema([asset("A1")], allow_generation=False)
    assert _PLAN_SCHEMA["properties"]["beats"]["items"]["properties"]["asset_id"] == before
    assert "asset_id" not in _PLAN_SCHEMA["properties"]["beats"]["items"]["required"]
