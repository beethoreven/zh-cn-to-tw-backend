"""決定性簡轉繁：用 OpenCC 的詞彙級轉換，不依賴 LLM，不會有隨機殘留或幻覺問題。"""

import opencc

_s2twp = opencc.OpenCC("s2twp")  # 簡體 -> 台灣正體，含慣用詞彙轉換（軟件->軟體、鼠標->滑鼠）


def convert_to_traditional(text: str) -> str:
    return _s2twp.convert(text)
