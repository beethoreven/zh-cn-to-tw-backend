# -*- mode: python ; coding: utf-8 -*-
# 把 zh-cn-to-tw-backend 打包成獨立執行檔，讓桌面版 App（zh-cn-to-tw-mac /
# zh-cn-to-tw-windows）可以把它跟 zh-cn-to-tw-ocr-service 一起包進 .app，
# 完全在使用者本機執行，不再依賴 Render。
#
# 打包後對外的網路連線只剩三個「本來就一定要連外」的服務：
#   1. Google（驗證登入用的 ID Token 簽章）
#   2. Neon（Postgres 資料庫）
#   3. Gemini / Anthropic（LLM API）
# 其餘（網頁介面、job 排程、OpenCC 簡繁轉換、docx 匯出）全部本機完成。
#
# 刻意排除 paddle/paddleocr/PyMuPDF：桌面版的 OCR 是由旁邊獨立的
# zh-cn-to-tw-ocr-service 負責，這支 backend 的 run_ocr_stage()（伺服器端
# 上傳流程專用）永遠不會被呼叫到。那幾個套件光 paddle 就約 600MB，且
# pipeline/orchestrator.py 已經把這些 import 改成放在函式內（見該檔說明），
# 這裡再明確 exclude 一次，確保 PyInstaller 不會因為任何間接引用又把它們
# 拉進來。
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

import os

# SPECPATH 是 PyInstaller 注入的內建變數（這個 .spec 檔所在的絕對路徑），
# 不要假設呼叫端的目前工作目錄。
repo_root = os.path.dirname(SPECPATH)

# opencc-python-reimplemented 的簡繁轉換設定檔與字典是純資料檔案
# （JSON + 詞庫），PyInstaller 的靜態分析看不到，一定要明確收進來，
# 否則轉換時會在執行期丟 FileNotFoundError。
opencc_datas = collect_data_files("opencc")

metadata_packages = [
    "Flask", "Flask-Cors", "anthropic", "google-genai", "google-auth",
    "psycopg2-binary", "opencc-python-reimplemented", "python-docx",
    "requests", "Pillow", "python-dotenv",
]
metadata_datas = []
for pkg in metadata_packages:
    try:
        metadata_datas += copy_metadata(pkg)
    except Exception:
        pass

hidden_imports = [
    # psycopg2 的 C 擴充模組，靜態分析常常追不到
    "psycopg2", "psycopg2._psycopg",
    # google-genai / google-auth 內部有動態 import
    "google.genai", "google.auth", "google.oauth2", "google.auth.transport.requests",
    "anthropic",
    "opencc",
    "docx",
    # Werkzeug/Flask 執行期才決定要用哪個 provider
    "werkzeug.middleware.proxy_fix",
]

a = Analysis(
    [os.path.join(repo_root, "app.py")],
    pathex=[repo_root],
    binaries=[],
    datas=opencc_datas + metadata_datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "paddle", "paddleocr", "paddlepaddle", "fitz", "pymupdf",
        "cv2", "scipy", "skimage", "sklearn", "matplotlib", "imgaug",
        "albumentations", "shapely", "lmdb", "pyclipper",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="zh-cn-to-tw-backend",
    debug=False,
    strip=False,
    upx=True,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="zh-cn-to-tw-backend",
)
