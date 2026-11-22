"""繁化結果文字 <-> .docx 互轉，供下載端點跟「直接上傳」功能使用。"""

from __future__ import annotations

import io

from docx import Document
from docx.oxml.ns import qn
from docx.text.hyperlink import Hyperlink


def text_to_docx_bytes(text: str) -> bytes:
    document = Document()
    # 每一行都當成獨立段落（Word 的「¶」），不是段落內的軟換行
    # （Word 的「↵」）——即使上游正規化過，這裡再切一次雙重保險，
    # 避免殘留的單一換行在 docx 裡變成看不出來的軟換行。
    for line in text.splitlines():
        if line.strip():
            document.add_paragraph(line)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def docx_file_to_text(file_stream) -> tuple[str, int]:
    """讀取上傳的 .docx 檔案，逐段落組回文字（雙換行分段，跟本專案
    其他地方的段落慣例一致）。回傳 (文字, 略過的隱藏文字字數)。

    設成「隱藏」的文字在 Word 裡看不到，卻原本會被讀進來、送給 LLM。劇本
    是第三方提供的，隱藏文字正好可以拿來夾帶寫給模型看的指示，使用者自己
    打開檔案也看不出來，所以一律略過，並把略過的字數交給呼叫端告訴使用者。"""
    document = Document(file_stream)
    defaults_hidden = _doc_defaults_hidden(document)
    paragraphs = []
    hidden_chars = 0
    for paragraph in document.paragraphs:
        pieces = []
        for item in paragraph.iter_inner_content():
            runs = item.runs if isinstance(item, Hyperlink) else [item]
            for run in runs:
                text = run.text
                if _run_is_hidden(run, paragraph, defaults_hidden):
                    hidden_chars += len(text)
                    continue
                pieces.append(text)
        paragraphs.append("".join(pieces))
    return "\n\n".join(paragraphs), hidden_chars


def _style_hidden(style) -> bool | None:
    """樣式（含 basedOn 繼承鏈）有沒有設定隱藏；都沒設定回傳 None。"""
    while style is not None:
        value = style.font.hidden
        if value is not None:
            return value
        style = style.base_style
    return None


def _doc_defaults_hidden(document) -> bool:
    vanish = document.styles.element.xpath("./w:docDefaults/w:rPrDefault/w:rPr/w:vanish")
    if not vanish:
        return False
    val = vanish[0].get(qn("w:val"))
    return val is None or val.lower() not in ("0", "false", "off")


def _run_is_hidden(run, paragraph, defaults_hidden: bool) -> bool:
    """這段文字在 Word 裡是不是看不到。

    直接套在文字上的格式優先：明確設成不隱藏就是看得到。沒有直接格式時，
    字元樣式、段落樣式、文件預設值只要有一層設成隱藏，就當成隱藏——Word 對
    「隱藏」這種切換屬性在多層樣式之間的算法更複雜，寧可多略過，也不要把
    看不到的字送出去；略過的字數會告訴使用者，真的少了內容看得出來。"""
    direct = run.font.hidden
    if direct is not None:
        return direct
    return bool(_style_hidden(run.style) or _style_hidden(paragraph.style) or defaults_hidden)
