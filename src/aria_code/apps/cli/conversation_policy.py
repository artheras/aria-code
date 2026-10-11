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
