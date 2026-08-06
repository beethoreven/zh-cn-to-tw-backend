"""文字正規化工具，統一換行/段落表示方式。"""

import re


def normalize_paragraph_breaks(text: str) -> str:
    """把任何連續的換行都正規化成統一的段落分隔符（雙換行）。

    OCR 逐行辨識，或 Gemini 潤飾後的文字，常常留下不上不下的單一換行
    （只是行與行之間斷行，不是真正的段落分隔）。這裡統一把它們都當作
    段落處理，確保輸出（無論 txt 還是 docx）看到的都是段落分隔，
    不會有 docx 裡「軟換行」跟「段落」混雜的問題。
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"\n+", "\n\n", normalized)
    return normalized.strip()
