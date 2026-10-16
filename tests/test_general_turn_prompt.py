"""A plain greeting on a local model is not answered with an offer of football.

On the Ollama path every general turn used to get one hard-coded prompt that
opened by listing what Aria can do — financial quant analysis, football and
sports prediction with a Poisson model, programming — and then a long section
of rules for sports data blocks. A small model told that offers it back: a bare
你好 was answered with "是关于足球赛事还是其他方面的问题呢？如果您有具体的
【泊松模型量化预测】块…", after sixteen seconds, to someone who had asked for
nothing of the kind.

The sports rules only mean something when sports data is attached to the
message, so they are now added only for a sports query.
"""
import pytest

from aria_code.apps.cli.providers.llm.ollama_stream import _general_turn_prompt

SPECIALIST = ("足球", "泊松", "football", "poisson", "量化", "金融", "sports")


@pytest.mark.parametrize("message", [
    "你好", "Hello!", "谢谢", "what is a reorder point?", "解释一下什么是闭包",
    "帮我想一个产品名字", "今天心情不错",
])
def test_an_ordinary_turn_is_not_told_about_any_specialist_domain(message):
    prompt = _general_turn_prompt(message).lower()
    for word in SPECIALIST:
        assert word.lower() not in prompt, (message, word)


def test_an_ordinary_turn_still_has_a_date_and_the_rules_every_turn_gets():
    prompt = _general_turn_prompt("你好")
    assert "Today is" in prompt
    assert "Answer in the user's language" in prompt


@pytest.mark.parametrize("message", [
    "世界杯 加拿大 vs 波黑 预测", "英超积分榜",
])
def test_a_sports_question_still_gets_the_sports_rules(message):
    # Narrowing the default must not remove what a real sports turn needs.
    prompt = _general_turn_prompt(message)
    assert "【泊松模型量化预测】" in prompt
    assert "不要质疑" in prompt


def test_the_sports_rules_are_added_to_the_neutral_prompt_not_instead_of_it():
    prompt = _general_turn_prompt("世界杯 加拿大 vs 波黑 预测")
    assert prompt.startswith(_general_turn_prompt("你好")), "the neutral part comes first"


def test_a_follow_up_in_a_sports_thread_keeps_the_rules_for_the_block_above_it():
    # "那加拿大呢?" is not a sports query on its own, but the history still
    # holds the prediction block the model has to keep treating as fact.
    history = [
        {"role": "user", "content": "世界杯 加拿大 vs 波黑 预测"},
        {"role": "assistant", "content": "【泊松模型量化预测】\n预期进球 1.4 : 0.9\n最可能比分 1-0"},
    ]
    assert "不要质疑" in _general_turn_prompt("那加拿大呢?", history)


def test_a_greeting_after_a_sports_thread_gets_the_rules_too_which_is_the_cost():
    # The history check cannot tell a follow-up from a change of subject, so a
    # 你好 inside a sports thread is still told about the block. Contained to
    # conversations that actually hold one, which is the point of the change.
    history = [{"role": "assistant", "content": "【泊松模型量化预测】预期进球 1.4 最可能比分 1-0"}]
    assert "不要质疑" in _general_turn_prompt("你好", history)
    assert "不要质疑" not in _general_turn_prompt("你好", [{"role": "user", "content": "hi"}])
