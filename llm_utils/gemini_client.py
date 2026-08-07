"""
Gemini API 層：Stage 1 潤飾 + Stage 2 校對都用這個 client，各自有各自的 prompt。

每次呼叫都是獨立、無狀態的 API request，不會有網頁聊天介面那種
session/快取殘留、越改越亂的問題。
"""

from __future__ import annotations

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from configs import config
from llm_utils.call_timeout import call_with_timeout
from llm_utils.errors import OutputTruncatedError, QuotaExceededError
from llm_utils.prompts import REFINE_PROMPT
from review.prompts import REVIEW_PROMPT
from usage.usage_log import record_usage

_client = None

# 保留這裡的名稱以維持既有 import 相容（orchestrator.py 等處引用）
__all__ = ["OutputTruncatedError", "refine_text", "review_text"]


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        if not config.GEMINI_API_KEY:
            raise RuntimeError("環境變數 GEMINI_API_KEY 未設定，無法呼叫 Gemini API")
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


def _generate(
    prompt: str,
    model: str | None,
    user_email: str | None,
    response_mime_type: str | None = None,
    file_name: str | None = None,
    project: int | None = None,
) -> str:
    client = _get_client()
    model_name = model or config.GEMINI_MODEL

    # 不論這次呼叫最後成功或失敗，都算「打了一次」，記錄下來當用量參考
    record_usage(model_name, user_email=user_email, file_name=file_name, project=project)

    gen_config = types.GenerateContentConfig(response_mime_type=response_mime_type) if response_mime_type else None
    try:
        response = call_with_timeout(
            config.LLM_CALL_TIMEOUT_SECONDS,
            client.models.generate_content,
            model=model_name,
            contents=prompt,
            config=gen_config,
        )
    except genai_errors.APIError as exc:
        if exc.code == 429 or exc.status == "RESOURCE_EXHAUSTED":
            raise QuotaExceededError(
                f"{model_name} 的 API 額度已用盡（429 RESOURCE_EXHAUSTED），"
                "重試同一個 model 沒有用，建議換一個 model 或等額度重置"
            ) from exc
        raise

    candidates = getattr(response, "candidates", None)
    finish_reason = candidates[0].finish_reason if candidates else None

    if finish_reason == "MAX_TOKENS":
        raise OutputTruncatedError(
            "Gemini 回覆被截斷（finish_reason=MAX_TOKENS），這批文字可能太長，"
            "超過模型單次回覆的輸出上限，建議調小批次頁數"
        )
    if finish_reason == "SAFETY":
        raise RuntimeError(
            "Gemini 因安全政策拒絕輸出這段內容（finish_reason=SAFETY）"
        )

    return response.text.strip()


def refine_text(
    text: str,
    model: str | None = None,
    user_email: str | None = None,
    file_name: str | None = None,
    project: int | None = None,
) -> str:
    prompt = REFINE_PROMPT.format(text=text)
    return _generate(prompt, model, user_email, file_name=file_name, project=project)


def review_text(
    text: str,
    model: str | None = None,
    user_email: str | None = None,
    file_name: str | None = None,
    project: int | None = None,
) -> str:
    """Stage 2 校對：回傳 LLM 原始輸出（應為 JSON 陣列字串），由呼叫端解析。"""
    prompt = REVIEW_PROMPT.format(text=text)
    return _generate(
        prompt, model, user_email, response_mime_type="application/json", file_name=file_name, project=project
    )
