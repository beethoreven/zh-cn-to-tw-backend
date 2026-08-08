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

CPU 執行緒數刻意限制成 1（見 _get_ocr）：Render 免費方案的 CPU 配額
非常低（官方文件是 0.1 顆），PaddleOCR 底層的 OpenMP/BLAS 預設會開
好幾條執行緒平行運算，在這種配額下只會互搶那一丁點 CPU、瘋狂 context
switch，不但沒有加速，還會讓整個 process（包含 Flask 主執行緒）長時間
無法回應任何請求，被 Render 的健康檢查判定「沒反應」直接砍掉重啟
（實測 log 症狀：PaddlePaddle 自己的內部監控元件 bvar 持續噴
「busy at sampling」，代表 CPU 已經被榨乾到連自己採樣都做不到）。

ocr_version 刻意指定成 "PP-OCRv3"（預設是較新的 PP-OCRv4）：實測在
Render 上曾經直接以 SIGILL（Illegal instruction）整個 process 崩潰，
C++ traceback 指向 PP-OCRv4 模型初始化時的 SelfAttentionFusePass
這個圖優化步驟——PP-OCRv4 的模型架構多了自注意力機制相關元件，這個
最佳化步驟疑似用到 Render 那台機器的 CPU 不支援的向量化指令。這也
解釋了為什麼有時候能跑、有時候不能：Render 免費方案的 instance 可能
被排到不同實體主機上，只有部分主機的 CPU 缺這個指令集。PP-OCRv3 是
較舊、架構更單純（沒有自注意力融合這個最佳化路徑）的官方模型版本，
用它來完全避開這個崩潰點，代價是辨識準確度可能略遜於 PP-OCRv4，但
「跑不起來」比「準確度差一點」更嚴重，先求穩定可用。
"""

import os
import threading

from PIL import Image

_ocr = None
_ocr_lock = threading.Lock()

_CPU_THREADS = int(os.environ.get("OCR_CPU_THREADS", "1"))


def _get_ocr():
    global _ocr
    if _ocr is None:
        # 這幾個環境變數要在 paddle 的原生函式庫真正被載入之前設好
        # （底層 OpenMP/MKL/OpenBLAS 是在函式庫初始化時讀取這些值，
        # 不是每次呼叫才讀），所以放在 import paddleocr 之前設定。
        os.environ.setdefault("OMP_NUM_THREADS", str(_CPU_THREADS))
        os.environ.setdefault("MKL_NUM_THREADS", str(_CPU_THREADS))
        os.environ.setdefault("OPENBLAS_NUM_THREADS", str(_CPU_THREADS))

        from paddleocr import PaddleOCR

        _ocr = PaddleOCR(
            use_angle_cls=True,
            lang="ch",
            show_log=False,
            cpu_threads=_CPU_THREADS,
            ocr_version="PP-OCRv3",
        )
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
