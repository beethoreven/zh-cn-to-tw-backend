"""LLM 處理後的長度上限，Stage 1 潤飾與 Stage 3 前置處理共用（門檻與理由
見 configs/config.py 的 REFINE_MAX_OUTPUT_RATIO）。"""

from __future__ import annotations

from configs import config


def content_length(text: str) -> int:
    """不算空白的字數：合併或拆開換行只改變空白，不是內容。"""
    return len("".join(text.split()))


def exceeds_max_output(original: str, processed: str) -> bool:
    """processed 是不是比 original 多出太多（超過比例上限加上固定寬限字數）。"""
    limit = content_length(original) * config.REFINE_MAX_OUTPUT_RATIO + config.REFINE_MAX_OUTPUT_SLACK_CHARS
    return content_length(processed) > limit
