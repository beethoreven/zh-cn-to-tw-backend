"""
Claude API 層，Stage 1 潤飾 + Stage 2 校對都用這個 client（Haiku/Opus
兩個選項，會實際計費，額度跟 Gemini 完全分開）。

跟 Gemini 一樣：每次呼叫都是獨立、無狀態的 API request；沒有配置
ANTHROPIC_API_KEY 的話會在第一次呼叫時丟出清楚的錯誤，不會靜默失敗。

每次呼叫都會把實際用掉的 input/output token 數記進 usage_log，用來算
「本月使用量」介面上顯示的參考費用。
"""

from __future__ import annotations

import anthropic

from configs import config
from llm_utils.errors import OutputTruncatedError
from llm_utils.prompts import REFINE_PROMPT
from review.prompts import REVIEW_PROMPT
from usage.usage_log import record_usage

_client = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError("環境變數 ANTHROPIC_API_KEY 未設定，無法呼叫 Claude API")
        # Anthropic SDK 原生支援 timeout，不需要像 Gemini 那樣自己包一層
        _client = anthropic.Anthropic(
            api_key=config.ANTHROPIC_API_KEY, timeout=config.LLM_CALL_TIMEOUT_SECONDS
        )
    return _client


def _generate(
    prompt: str, model: str | None, user_email: str | None, file_name: str | None = None
) -> str:
    client = _get_client()
    model_name = model or "claude-haiku-4-5-20251001"

    response = client.messages.create(
        model=model_name,
        max_tokens=config.CLAUDE_MAX_OUTPUT_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )

    usage = getattr(response, "usage", None)
    record_usage(
        model_name,
        user_email=user_email,
        input_tokens=getattr(usage, "input_tokens", None) if usage else None,
        output_tokens=getattr(usage, "output_tokens", None) if usage else None,
        file_name=file_name,
    )

    if response.stop_reason == "max_tokens":
        raise OutputTruncatedError(
            "Claude 回覆被截斷（stop_reason=max_tokens），這批文字可能太長，"
            "超過模型單次回覆的輸出上限，建議調小批次大小"
        )

    return "".join(block.text for block in response.content if block.type == "text").strip()


def refine_text(
    text: str, model: str | None = None, user_email: str | None = None, file_name: str | None = None
) -> str:
    prompt = REFINE_PROMPT.format(text=text)
    return _generate(prompt, model, user_email, file_name=file_name)


def review_text(
    text: str, model: str | None = None, user_email: str | None = None, file_name: str | None = None
) -> str:
    """Stage 2 校對：回傳 Claude 原始輸出（應為 JSON 陣列字串），由呼叫端解析。"""
    prompt = REVIEW_PROMPT.format(text=text)
    return _generate(prompt, model, user_email, file_name=file_name)
