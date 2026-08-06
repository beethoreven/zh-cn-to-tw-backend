"""
封面頁偵測：純粹用 PIL 對已經 render 好的頁面圖片做統計，抓兩個訊號：
1. 墨水覆蓋率——純文字頁通常大量留白，覆蓋率偏低；整頁圖片/照片通常
   整頁都有內容，覆蓋率明顯偏高。
2. 顏色飽和度——文字掃描頁通常接近灰階（飽和度低），有色彩的封面
   插畫/照片飽和度通常明顯偏高。

這是啟發式規則，不是影像辨識模型，不需要額外的付費 API 或另外裝模型，
用現有的 Pillow 就能做，完全免費、本機運算、不花時間也不花錢。代價是
會有誤判：太花俏的文字頁（例如彩色標題頁）可能被誤判成封面，純黑白的
封面插畫也可能因為飽和度不夠而漏判。所以介面上這個功能設計成使用者
可以自己關掉的開關，不是強制行為。
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image


@dataclass
class CoverPageResult:
    is_cover: bool
    dark_ratio: float
    saturation: float
    reference_dark_ratio: float | None
    reference_saturation: float | None


def _dark_ratio(img: Image.Image, threshold: int = 200) -> float:
    """回傳圖片裡「較暗」像素的比例（灰階值 < threshold）。"""
    gray = img.convert("L")
    histogram = gray.histogram()
    total_pixels = gray.width * gray.height
    if not total_pixels:
        return 0.0
    dark_pixels = sum(histogram[:threshold])
    return dark_pixels / total_pixels


def _mean_saturation(img: Image.Image) -> float:
    """回傳圖片的平均飽和度（0-255）。"""
    hsv = img.convert("HSV")
    _, saturation_channel, _ = hsv.split()
    histogram = saturation_channel.histogram()
    total_pixels = saturation_channel.width * saturation_channel.height
    if not total_pixels:
        return 0.0
    weighted_sum = sum(value * count for value, count in enumerate(histogram))
    return weighted_sum / total_pixels


def evaluate_cover_page(
    first_page: Image.Image,
    reference_page: Image.Image | None,
    dark_ratio_threshold: float,
    saturation_threshold: float,
    relative_margin: float,
) -> CoverPageResult:
    """粗略判斷 first_page 是不是「含圖片的封面頁」，回傳判斷結果連同算出來
    的數值，方便呼叫端把「為什麼這樣判斷」完整記進 log，不是只給一個
    布林值黑盒結果。

    只要墨水覆蓋率或飽和度其中一項超過絕對門檻，或明顯高於 reference_page
    （通常是第二頁，用來當「正常內文長怎樣」的參考基準）乘上 relative_margin
    倍，就判定為封面。任一訊號夠強即可觸發（OR），因為封面可能是彩色插畫
    （飽和度訊號強）也可能是黑白照片/滿版圖（墨水覆蓋率訊號強），不強求
    兩個訊號同時出現。
    """
    dark_ratio = _dark_ratio(first_page)
    saturation = _mean_saturation(first_page)
    ref_dark_ratio = _dark_ratio(reference_page) if reference_page is not None else None
    ref_saturation = _mean_saturation(reference_page) if reference_page is not None else None

    is_cover = dark_ratio >= dark_ratio_threshold or saturation >= saturation_threshold
    if not is_cover and reference_page is not None:
        if ref_dark_ratio > 0 and dark_ratio >= ref_dark_ratio * relative_margin:
            is_cover = True
        elif ref_saturation > 0 and saturation >= ref_saturation * relative_margin:
            is_cover = True

    return CoverPageResult(
        is_cover=is_cover,
        dark_ratio=dark_ratio,
        saturation=saturation,
        reference_dark_ratio=ref_dark_ratio,
        reference_saturation=ref_saturation,
    )
