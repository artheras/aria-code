"""OpenAI's API wants max_completion_tokens and a default temperature on its reasoning models; imitators do not."""
import pytest

from aria_code.providers.openai_params import is_openai_api, sampling_params


@pytest.mark.parametrize("url,official", [
    ("https://api.openai.com/v1/chat/completions", True), ("https://api.openai.com", True),
    ("https://eu.api.openai.com/v1", True), ("https://api.deepseek.com/chat/completions", False),
    ("https://api.groq.com/openai/v1/chat/completions", False), ("http://localhost:11434/v1", False),
    ("https://notopenai.com/v1", False), ("https://openai.com.evil.example/v1", False), ("", False),
])
def test_only_openais_own_hosts_count_as_openai(url, official):
    assert is_openai_api(url) is official


def test_openai_gets_max_completion_tokens():
    p = sampling_params("https://api.openai.com/v1/chat/completions", "gpt-4o", 0.2, 4096)
    assert p == {"temperature": 0.2, "max_completion_tokens": 4096}


@pytest.mark.parametrize("model", ["o1", "o3-mini", "o4-mini", "gpt-5", "gpt-5.4-mini", "gpt-6.1-sol", "gpt-10"])
def test_reasoning_models_get_no_temperature(model):
    p = sampling_params("https://api.openai.com/v1/chat/completions", model, 0.2, 4096)
    assert p == {"max_completion_tokens": 4096}


@pytest.mark.parametrize("model", ["gpt-4o", "gpt-4.1-mini", "chat-latest"])
def test_older_chat_models_keep_their_temperature(model):
    assert "temperature" in sampling_params("https://api.openai.com/v1", model, 0.2, 100)


def test_servers_that_imitate_openai_are_left_exactly_as_before():
    for url in ("https://api.deepseek.com/v1/chat/completions", "https://api.groq.com/openai/v1/chat/completions",
                "http://localhost:11434/v1/chat/completions"):
        assert sampling_params(url, "gpt-5-like-name", 0.3, 2048) == {"temperature": 0.3, "max_tokens": 2048}


def test_a_missing_limit_or_temperature_is_simply_absent():
    assert sampling_params("https://api.openai.com/v1", "gpt-4o", None, None) == {}
