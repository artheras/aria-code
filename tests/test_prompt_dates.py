from datetime import datetime
import pytest
from aria_code.apps.cli.prompts import system_prompts as prompts


class EnglishLocaleDate(datetime):
    @classmethod
    def now(cls):
        return cls(2026, 10, 9)

    def strftime(self, fmt):
        fmt.encode("ascii")  # emulate a Windows locale unable to encode 年/月/日
        return super().strftime(fmt)


@pytest.mark.parametrize("build,args", [
    (prompts.build_coding_prompt_lite, ("你好",)),
    (prompts.build_analysis_prompt_lite, ("你好",)),
    (prompts.build_finance_prompt, ("你好",)),
    (prompts.build_prefetched_analysis_prompt, ()),
])
def test_chinese_dates_do_not_go_through_locale_encoded_strftime(monkeypatch, build, args):
    monkeypatch.setattr(prompts, "_dt", EnglishLocaleDate)
    assert "2026年10月09日" in build(*args)
