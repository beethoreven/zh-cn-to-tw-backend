"""把 PDF 逐頁轉成圖片，供 OCR 辨識使用。"""

from __future__ import annotations

import fitz  # PyMuPDF
from PIL import Image

from configs import config


def render_pdf_pages(pdf_path: str, dpi: int | None = None) -> list[Image.Image]:
    """回傳每一頁的 PIL Image，依原始頁序排列。"""
    images = []
    zoom = (dpi or config.PDF_RENDER_DPI) / 72  # PDF 預設 72 DPI，換算縮放倍率
    matrix = fitz.Matrix(zoom, zoom)

    with fitz.open(pdf_path) as doc:
        for page in doc:
            pix = page.get_pixmap(matrix=matrix)
            mode = "RGB" if pix.n < 4 else "RGBA"
            img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
            images.append(img.convert("RGB"))

    return images
