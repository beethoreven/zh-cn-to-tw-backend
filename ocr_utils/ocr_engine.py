"""
PaddleOCR 包裝層。

模型只在第一次呼叫時載入一次（很吃時間跟記憶體），之後重複使用同一個
instance。辨識結果依文字框的座標排序，確保輸出順序符合正常閱讀順序
（由上到下、同一行由左到右），而不是 PaddleOCR 內部偵測到的順序。

用一個 lock 把實際辨識呼叫序列化：如果兩個使用者同時各自上傳一份
PDF，各自的處理在各自的背景執行緒裡跑，但底層共用同一個 PaddleOCR
instance——這個 instance 對「多執行緒同時呼叫 .ocr()」是否安全並沒有
明確保證，加鎖是保守但正確的做法，避免潛在的相互干擾或崩潰，代價是
兩份文件的 OCR 階段會排隊序列跑，不是真正平行（但 OCR 只是整體流程
的一部分，Gemini 潤飾那段呼叫不受這個鎖影響，仍然可以同時進行）。
"""

import threading

from PIL import Image

_ocr = None
_ocr_lock = threading.Lock()


def _get_ocr():
    global _ocr
    if _ocr is None:
        from paddleocr import PaddleOCR

        _ocr = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
    return _ocr


def _line_key(item):
    box = item[0]
    top = min(p[1] for p in box)
    left = min(p[0] for p in box)
    return (top, left)


def ocr_page(image: Image.Image) -> str:
    """辨識單一頁面圖片，回傳依閱讀順序組合的文字（保留原始簡體，尚未轉繁）。"""
    import numpy as np

    ocr = _get_ocr()
    with _ocr_lock:
        result = ocr.ocr(np.array(image), cls=True)

    if not result or not result[0]:
        return ""

    lines = result[0]
    lines_sorted = sorted(lines, key=_line_key)
    texts = [line[1][0] for line in lines_sorted]
    return "\n".join(texts)
