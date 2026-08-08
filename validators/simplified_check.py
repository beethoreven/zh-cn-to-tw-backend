"""
偵測文字中是否還殘留簡體字。

作法：把文字拿去跑一次 convert_utils 實際使用的同一套轉換設定（s2twp），
如果結果跟原文不同，代表原文裡還有字會被這個轉換動作改掉——也就是還殘留
簡體字。已經是正確繁體的文字不會再被同一套設定改動，所以這個檢查不會誤判
乾淨的繁體文字。

注意：這裡一定要跟 convert_utils.opencc_convert 用同一個 profile（s2twp），
不能換成單純的 s2t——s2t 對某些異體字（例如 秘/祕、里/裡/裏）的偏好跟
台灣標準的 s2twp 不同，拿 s2t 當驗證標準會把 s2twp 已經正確轉換好的字
（例如「秘密」）誤判成殘留簡體字，導致 retry 永遠卡在同一個字上。
"""

import difflib

import opencc

_s2twp = opencc.OpenCC("s2twp")


def find_residual_simplified(text: str) -> list[str]:
    """回傳文字中偵測到的殘留簡體字元清單（去重，依出現順序）。

    用 difflib 逐段比對 text 跟轉換結果，而不是直接 zip 逐字元比對——
    s2twp 有些是詞組級的轉換（例如「内存」2 字轉成「記憶體」3 字），
    一旦轉換前後長度不同，zip 會在長度不同的那個位置之後整段錯位，
    導致後面本來沒問題的字被誤判成殘留、也可能讓真正殘留的字因為
    比對到錯誤的位置而漏掉。difflib 的 opcode 是照實際插入/刪除/取代
    的區段對齊，不會有這個問題。
    """
    converted = _s2twp.convert(text)
    if converted == text:
        return []

    seen = set()
    residual = []
    matcher = difflib.SequenceMatcher(a=text, b=converted, autojunk=False)
    for tag, i1, i2, _j1, _j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        for char in text[i1:i2]:
            if char not in seen:
                seen.add(char)
                residual.append(char)
    return residual
