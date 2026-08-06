"""
幫沒有內建逾時機制的 SDK 呼叫加上逾時保護。

實際發生過的案例：Gemini API 呼叫卡住，永遠沒有回應也沒有拋出例外，
導致整個批次卡死、使用者看畫面以為「當掉了」。用一個共用的 thread
pool 執行實際呼叫，用 future.result(timeout=...) 限制最多等多久，
逾時就當作這次呼叫失敗（讓既有的 retry/fallback 機制接手）。

注意：Python 沒有辦法真的「殺掉」一個卡住的執行緒，逾時之後底層那個
卡住的網路請求仍然會在背景繼續等，直到它自己逾時或完成為止，但這不
影響其他請求——因為每次呼叫都是丟到 thread pool 的獨立工作，不會
互相阻塞。
"""

from __future__ import annotations

import concurrent.futures

_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="llm-call"
)


class CallTimeoutError(RuntimeError):
    """API 呼叫超過設定的逾時時間仍未回應。"""


def call_with_timeout(timeout_seconds: float, fn, *args, **kwargs):
    future = _executor.submit(fn, *args, **kwargs)
    try:
        return future.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError as exc:
        raise CallTimeoutError(f"API 呼叫超過 {timeout_seconds} 秒沒有回應（逾時）") from exc
