"""Every task is told to write the files it is asked for, compute numbers with code,
and keep a client's document to that client's data.

In the Vertex evals the agent reported reorder.json's contents without writing
it, and added up a shipper's stock by hand (500 for 560). Two of those prompts
read as general chat and got the short general prompt, with no tool discipline.
"""

from __future__ import annotations

from aria_code.apps.cli.prompts.coding import CODING_SYSTEM_PROMPT
from aria_code.apps.cli.prompts.select import OUTPUT_DISCIPLINE, build_turn_system_prompt, select_base_prompt


def test_tasks_and_general_questions_get_the_output_rules():
    for message in ("fix the failing test", "AAPL price", "what can you help me do?", "what is a reorder point", "hello there, read README.md"):
        assert OUTPUT_DISCIPLINE in build_turn_system_prompt(message)


def test_standalone_social_messages_do_not_request_file_or_computation_work():
    for message in ("hello there", "你好", "thank you"):
        assert OUTPUT_DISCIPLINE not in build_turn_system_prompt(message)


def test_the_rules_say_what_the_evals_needed():
    assert "confirm it exists" in OUTPUT_DISCIPLINE
    assert "not by hand" in OUTPUT_DISCIPLINE


def test_a_client_document_holds_only_that_clients_data():
    # gemini-3.5-flash, operations suite: report_BETA.md named Acme and
    # Gamma and their SKUs — copied from notes on Beta's own rows.
    assert "only their data" in OUTPUT_DISCIPLINE
    assert "inside this client's own rows" in OUTPUT_DISCIPLINE


def test_work_on_files_in_this_folder_gets_the_coding_prompt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "inventory.csv").write_text("owner_id,sku,on_hand\n")
    message = "给货主 BETA 准备一份本周库存简报，写到 report_BETA.md。数据在 inventory.csv。"
    assert select_base_prompt(message) == CODING_SYSTEM_PROMPT


def test_a_general_question_keeps_the_general_prompt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert select_base_prompt("what is a reorder point?") != CODING_SYSTEM_PROMPT


def test_an_override_is_left_alone():
    assert build_turn_system_prompt("x", override="only this") == "only this"


def test_a_greeting_prompt_does_not_name_a_specialist_domain():
    # The general prompt used to say "Finance and sports analysis are optional
    # domain capabilities, used only when the user asks for them." Meant as
    # neutrality, it did the opposite: a model told about two named
    # capabilities offers them back, and a plain 你好 was answered with an offer
    # of football and quantitative analysis. Specialist guidance is added by
    # the packs a message activates, so the general prompt has no reason to
    # name any domain.
    from aria_code.apps.cli.prompts.select import _general_prompt

    prompt = _general_prompt().lower()
    for domain in ("finance", "sports", "football", "quant", "stock", "trading", "market"):
        assert domain not in prompt, domain
    assert "never list or offer" in prompt


def test_a_greeting_gets_the_general_prompt_not_a_domain_one():
    prompt = build_turn_system_prompt("你好")
    assert "quantitative" not in prompt.lower()
    assert "football" not in prompt.lower()
