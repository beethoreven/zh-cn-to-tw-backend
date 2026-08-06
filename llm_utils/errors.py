"""LLM 呼叫共用的例外類型，Gemini/Claude client 共用。"""


class OutputTruncatedError(RuntimeError):
    """LLM 回覆因為輸出長度超過模型單次輸出上限被截斷，內容不可信任。"""


class QuotaExceededError(RuntimeError):
    """API 額度（RPD/RPM 或預付額度）已經用盡，這不是暫時性錯誤，
    重試同一個 model 沒有意義，除非等額度重置或換一個 model。"""
