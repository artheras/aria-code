"""Recognize standalone social messages without guessing a task's intent.

Matching the whole message is intentional: ``hi, read README.md`` must keep
its tools, as must ``yes`` or ``continue`` in an ongoing coding conversation.
"""
from __future__ import annotations

import re

_SOCIAL = frozenset({
    "hi", "hello", "hey", "hiya", "hi there", "hello there", "how are you", "how are you doing", "good morning", "good afternoon", "good evening",
    "hi aria", "hello aria", "hey aria", "thanks", "thank you", "thanks aria",
    "bye", "goodbye", "你好", "你好啊", "你好呀", "您好", "嗨", "哈喽", "在吗", "你在吗", "早安", "晚安", "早上好", "下午好", "晚上好", "👋",
    "你好aria", "aria你好", "你好 aria", "aria 你好", "hiaria", "helloaria", "heyaria", "谢谢", "谢谢你", "再见",
})


def is_social_message(message: str) -> bool:
    text = re.sub(r"[!！?？。，,.~～]+", "", str(message or "").strip().casefold())
    text = " ".join(text.split())
    return text in _SOCIAL


def is_capability_question(message: str) -> bool:
    """Standalone questions about the assistant, not requests to act on a subject.

    Full matching keeps a mixed request such as "what can you do; analyze AAPL"
    on the normal execution path. Mentioning stocks in a capability question
    does not ask for a current financial conclusion.
    """
    text = " ".join(str(message or "").strip().casefold().split()).rstrip("?？!！。.")
    chinese = re.sub(r"\s+", "", text)
    chinese = re.sub(r"^(?:除了|除)[^。！？!?;；]+?[，,]((?:你|您|aria(?:code)?).+)$", r"\1", chinese)
    if re.fullmatch(
        r"(?:你|您|aria(?:code)?)(?:(?:除了|除)[^。！？!?;；]+?(?:之外|以外|外)?[，,]?)?"
        r"还?(?:能|可以|会)(?:帮我|帮助我)?(?:做|干)(?:些|一些|点)?(?:什么|啥)(?:事情|工作|任务)?(?:吗|呢|呀|啊)?",
        chinese,
    ):
        return True
    if re.fullmatch(r"(?:你|您|aria(?:code)?)(?:还)?(?:有|具备|支持)(?:哪些|什么)(?:能力|功能|用途)(?:吗|呢)?", chinese):
        return True
    if re.fullmatch(r"(?:介绍|说明)(?:一下)?(?:你|您|aria(?:code)?)(?:的)?(?:能力|功能|用途)", chinese):
        return True
    return bool(re.fullmatch(
        r"(?:(?:besides|apart from|other than) [^.!?;]+[,，]? )?"
        r"what (?:else |other things )?can you (?:(?:help me )?(?:do|help with)|help me with)"
        r"(?: (?:besides|apart from|other than) [^.!?;]+)?"
        r"|what are your (?:capabilities|features)", text,
    ))


def is_conversation_only_message(message: str) -> bool:
    return is_social_message(message) or is_capability_question(message)
