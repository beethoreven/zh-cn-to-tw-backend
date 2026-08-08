"""把 PDF 逐頁轉成圖片，供 OCR 辨識使用。"""

from __future__ import annotations

from collections.abc import Iterator

import fitz  # PyMuPDF
from PIL import Image

from configs import config


def get_pdf_page_count(pdf_path: str) -> int:
    """只讀取頁數，不渲染任何一頁——呼叫端要在還沒實際轉圖之前就先知道
    總頁數（例如記 log、算進度）時用這個，不要為了拿頁數就把整份 PDF
    轉完。"""
    with fitz.open(pdf_path) as doc:
        return doc.page_count


def render_pdf_pages(pdf_path: str, dpi: int | None = None) -> Iterator[Image.Image]:
    """逐頁 yield PIL Image，依原始頁序排列。

    刻意做成產生器、不是一次回傳整個 list：如果把整份 PDF 每一頁的圖
    同時留在記憶體裡，頁數多、DPI 高的劇本很容易吃掉幾百 MB 到超過
    1GB，Render 免費方案的記憶體上限（512MB）撐不住，會被直接 OOM
    砍掉整個 process（連帶讓所有還在進行中的請求，包括前端輪詢，一起
    失敗）。呼叫端應該一次處理一頁（轉圖 → OCR → 丟棄這個 Image）就
    繼續下一頁，不要自己把整個產生器塞回 list。"""
    zoom = (dpi or config.PDF_RENDER_DPI) / 72  # PDF 預設 72 DPI，換算縮放倍率
    matrix = fitz.Matrix(zoom, zoom)

    with fitz.open(pdf_path) as doc:
        for page in doc:
            pix = page.get_pixmap(matrix=matrix)
            mode = "RGB" if pix.n < 4 else "RGBA"
            img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
            yield img.convert("RGB")
