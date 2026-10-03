# 中文

## 劇本殺繁化助手 — 後端 API

劇本殺（謀殺之謎）劇本簡體中文轉繁體中文的輔助工具，後端 API。這份文件分成兩個獨立的部分，請依需求閱讀:

- **[專案報告](#專案報告)**:這個系統是什麼、怎麼串起來的、用了哪些技術與決策 —— 給想了解「這是什麼」的人看。
- **[架設 SOP](#架設-sop)**:一步一步的操作說明 —— 給想「動手把它跑起來」的人看。

這兩部分刻意分開，不要交叉閱讀;報告是背景知識，SOP 是操作手冊。

---

## 專案報告

### 這是什麼

主持人／劇本負責人拿到的劇本殺劇本經常是簡體中文 PDF，需要先轉成台灣慣用的繁體中文才能給玩家使用。這個工具把「OCR 辨識 → 簡轉繁 → LLM 潤飾 → 人工校對」這條原本要手工做的流程自動化，分成兩個階段:

- **Stage 1（簡轉繁）**:上傳 PDF，自動 OCR + 簡轉繁 + LLM 潤飾（斷句、標點、修正 OCR 誤植錯字），輸出可下載的 .txt/.docx。
- **Stage 2（校對）**:對 Stage 1 的輸出（或直接上傳已經是繁體的檔案）再跑一輪 LLM 校對，抓用詞/錯字/標點問題，結構化清單讓使用者逐筆勾選要不要套用。

人工校對依然是必須的，但這個工具省下大部分前置作業的人力。

### 系統架構

整個專案分成四個獨立 repo，透過一個 git meta-repo（`zh-cn-to-tw`）用 submodule 掛在一起（見頂層 README 的說明）:

```
zh-cn-to-tw/                     ← meta-repo，本機開發統一入口，本身不部署
├── zh-cn-to-tw-backend/         ← 本 repo，部署到 Render(Flask)
├── zh-cn-to-tw-web/             ← 前端，main 分支被桌面版 App 內嵌；GitHub Pages 服務的是另一個獨立的 update-page 分支（純佔位頁）
├── zh-cn-to-tw-mac/             ← macOS 桌面殼（Swift/SwiftUI + WKWebView）
└── zh-cn-to-tw-ocr-service/     ← 只在使用者本機跑的 OCR 服務，被桌面殼當子行程拉起
```

這個架構是**演化來的，不是一開始就設計成這樣**——最初版本很單純：Render 後端接一切（含 OCR），瀏覽器直接打開 GitHub Pages 用。後來實測 Render 免費方案（0.1 CPU、512MB RAM）完全扛不住 PaddleOCR（見下方「為什麼 OCR 搬到使用者本機」），才逐步演變出桌面版這個做法。桌面版現在是**唯一的使用方式**：

- **桌面版**(zh-cn-to-tw-mac):OCR 在使用者自己的機器上跑（11+ 走 `zh-cn-to-tw-ocr-service`，10.15 走 Apple Vision framework），只把 OCR 完的簡體文字傳給這支 backend 做簡轉繁/LLM 潤飾(`POST /api/jobs/from-ocr-text`)，backend 完全不碰 PDF。
- **舊的瀏覽器版路徑已經拔掉**（2026-10-03）:PDF 直接上傳給這支 backend、OCR 在 Render 上跑的 `POST /api/jobs`，連同 `run_ocr_stage`、`ocr_utils/` 與 paddle 相關套件一起移除，原因見下方「拔掉伺服器端 OCR、升級到 Python 3.12」。前端在瀏覽器模式下按 Stage 1 只會提示要改用桌面版。

### 為什麼 OCR 搬到使用者本機（重大架構決策）

這是整個專案最大的一次轉向，過程踩了不少坑:

1. **PP-OCRv4 在 Render 上直接 SIGILL 崩潰**——特定主機的 CPU 不支援這個模型版本用到的向量化指令。改用 PP-OCRv3 繞過。
2. **PaddleOCR 光是第一次推論就把 RSS 衝到 2.6-2.8GB**，遠超過 Render 免費方案的 512MB 上限，OOM 被系統強制關閉。
3. **CPU 配額耗盡被判定無回應**——免費方案只有 0.1 CPU，OCR 這種吃 CPU 的工作很容易把單一請求的處理時間拖到平台判定逾時。
4. 升級付費方案在這個低毛利、個人使用規模的專案上不現實。

嘗試過的緩解方式（都只是延後問題，沒有根治）:限制 PaddleOCR 只用單一 CPU 執行緒、改成逐頁串流處理避免一次性記憶體尖峰、調降 DPI。**真正的解法是把 OCR 整個搬離 Render**——桌面版把這一段丟到使用者自己的機器上執行（`zh-cn-to-tw-ocr-service`），backend 只保留 DB、LLM API 金鑰、登入、管理員介面、job/review 狀態這些「一定要連外」的部分。

**曾經考慮過、後來放棄的方案**:讓桌面版把整支 backend 也一起打包進 .app（`packaging/backend_service.spec`，已刪除），本機自己跑一份完整的 backend。放棄原因是本機 backend 用 HTTP 供應網頁時，監聽的 port 是每次啟動隨機配的（刻意避免舊 process 卡住固定 port），而 `localStorage` 是照 `scheme+host+port` 算 origin 的——port 每次不一樣，等於使用者每次開 App 登入的 session 都救不回來，變成每次啟動都要重新登入。最後改成桌面殼直接用固定的 `file://` 路徑內嵌網頁（不透過任何本機 HTTP 伺服器），origin 因此穩定；憑證（DB 連線字串、LLM API 金鑰）完全不進桌面版 App，一律留在 Render——這是刻意的安全取捨:客戶端的機密本質上無法真正保護（程式執行時必須能解密才能用，加密只是提高門檻），所以憑證乾脆不下發，桌面版只拿一個範圍有限、可撤銷的 session token。

### 拔掉伺服器端 OCR、升級到 Python 3.12（2026-10-03）

OCR 搬到桌面版之後，伺服器端那條路徑（`POST /api/jobs` → `run_ocr_stage` → PaddleOCR）一直沒有跟著拆。實務上已經沒有人會走到它，但它留下兩個實際的代價:

1. **每次部署都要裝 paddle**:`requirements.txt` 裡的 paddlepaddle、paddleocr、PyMuPDF、Pillow 只有這條路徑用得到。
2. **它把 Python 釘死在 3.9**:`.python-version` 寫 3.9 的原因是 paddlepaddle 2.6.2 沒有更新版 Python 的 wheel（commit `7e2a1bc`）。這又連帶把 `google-genai` 卡在 0.3.0（從第一個 commit 起就沒升過）——google-genai 從 1.48.0 起要求 Python 3.10 以上，3.9 最高只能到 1.47.0，而 1.47.0 沒有 Gemini 3 系列用的 `thinking_level` 設定。

觸發這次清理的是 Gemini 3.6 Flash 反覆回 `503 UNAVAILABLE`（「This model is currently experiencing high demand」）:同一時段 Flash Lite 都秒回，3.6 Flash 卻 3 個工作共 9 次呼叫全部 503。直接打 REST API 對照:一句短 prompt 成功;真實 prompt（約 4,200 字）在預設思考程度下 503;只送一半長度一樣 503;把 `thinkingLevel` 設成 `low` 則成功——失敗的關鍵看起來是思考程度，不是長度（low 那組只測過 1 次，樣本很少）。要在程式碼裡設定思考程度，就得先升級 SDK，也就得先拔掉 paddle。思考程度目前**刻意沒有改**，仍是模型預設值;之後要調整時，SDK 已經支援。

升級連帶的版本變動:

- `google-genai` 0.3.0 → 2.27.0。
- `google-auth` 2.32.0 → 2.59.1:新版 google-genai 要求 `google-auth>=2.56`。這個套件同時負責 Google 登入驗證（`auth_utils/auth.py` 的 `verify_oauth2_token`），升級後用真實帳號登入驗證過。
- `cryptography` 釘在 48.0.1:新版 google-auth 帶進來的相依。49.0 起不再提供 macOS Intel（x86_64）的 wheel，在 Intel Mac 上 pip 會改成從原始碼編譯（需要 Rust）而卡住;48.0.1 是 macOS Intel 跟 Linux 都有 wheel 的最後一版，釘住讓本機跟 Render 裝的是同一個版本。

上線前的驗證不碰正式 DB、也不用先部署:用 3.12 建全新的 venv、`DATABASE_URL` 指到連不上的位址，直接呼叫路由與 Stage 1/2 流程、打真的 LLM（Flash Lite、Haiku、3.6 Flash 的 503 有被正確歸類成暫時性錯誤、Stage 2 的 JSON 模式、假 token 被拒絕）;再用 `WEB_API_BASE_OVERRIDE` 讓桌面版 App 連本機 backend，用真實帳號驗證登入與 Stage 1（見架設 SOP「Part A 6. 讓桌面版 App 連本機 backend」）。

### 資料庫與 Render 必須同區（2026-08-25 才發現的重大效能問題）

**Neon project 的區域一定要跟 Render service 的區域一致。** 目前兩邊都是 AWS `ap-southeast-1`（新加坡）。這不是最佳化建議，是這個系統效能的主要決定因素。

原本 Render 在新加坡、Neon 卻建在 `us-east-2`（俄亥俄），每次建立資料庫連線都要跨太平洋往返六趟。實測結果:不碰資料庫的 `/api/health` 回應 0.11 秒，只做一個 `COUNT`、不需要登入的 `/api/jobs/active` 卻要 1.76 秒，而且**重複呼叫不會變快**（排除了冷啟動）。更刺眼的是，開發者家用寬頻從台灣連到俄亥俄（199.7ms）比 Render 機房連到自己的資料庫（約 236ms）還快——**機房輸給家用網路，本身就是拓撲有問題的訊號**。

拆穿它的方法是一個比值:用 socket 量純 TCP 握手（`SYN→SYN/ACK` 定義上恰好一個往返）得到 199.7ms，而建立一條 psycopg2 連線是 1228ms，`1228 ÷ 199.7 = 6.15`——剛好對應 TCP(1) + TLS(1-2) + SCRAM(約3) 的往返次數。**商數落在小整數上，代表這個操作裡幾乎沒有運算，全部是網路往返**，而往返的價格由距離決定。這個比值同時排除了另一個競爭假設（Render 免費方案 0.1 CPU 把加密運算節流了），因為那會讓商數遠大於 6。

把資料庫搬到新加坡之後（扣掉 `/api/health` 當基準線）:

| | 遷移前 | 遷移後 |
|---|---|---|
| 純資料庫成本 | 1.76 秒 | **0.03 秒** |
| `/api/jobs/active` | 1.78–1.99 秒 | **0.12–0.21 秒** |

約 **59 倍**。新資料庫獨立驗證了同一個比值（`connect ÷ RTT = 5.98`），兩條完全不同的網路路徑都命中 6，所以那是可重複的判準而不是巧合。

**既有 Neon project 不能改區域**（官方明文），必須在目標區新建 project 再搬資料。搬遷本身很單純:整個資料庫只有 9.6MB、1606 列，而且**客戶端完全不持有資料庫憑證**（前端／Mac／Windows 一律打 Render API），所以要改的只有 Render 的環境變數跟本機 `.env`。

因為距離已經不是瓶頸，兩項原本被排定的最佳化都**刻意不做**:psycopg3 連線池（同區後只能再省約 130ms，卻要引進「Neon 閒置 5 分鐘 suspend 會砍掉池內連線」這個新失效模式），以及把每個已登入請求的 5 條連線減成 2 條（150ms → 60ms，對使用者無感）。完整推論見 `db_utils/connection.py` 的歷史教訓四。

### 檔案結構

```
app.py                  路由入口
configs/config.py       集中管理設定值與環境變數
db_utils/                資料庫層
  ├── connection.py       連線管理（見檔案開頭的說明）
  ├── schema.py            建表/遷移
  ├── job_store.py         job/review 狀態持久化到 Neon
  └── app_versions.py      桌面版 App 強制更新門檻查詢
auth_utils/               Google 登入驗證、session、白名單
jobs/ review/             Stage 1/Stage 2 各自的工作管理
pipeline/orchestrator.py  Stage 1 的簡轉繁→潤飾 流程編排（不含 OCR，OCR 一律在使用者本機做）
llm_utils/                Gemini/Claude 呼叫封裝
usage/                    用量統計（Gemini 次數、Claude token/費用）
admin_utils/              管理員介面（使用者/權限/專案管理）
output_utils/             輸出成 .txt/.docx
convert_utils/            OpenCC 簡轉繁
validators/               輸出驗證（例如殘留簡體字檢查）
```

### 已知限制

- `usage.db`（現在是 Neon 裡的 `usage_log` 表）記的是「本工具打了幾次」，不是 Google/Anthropic 官方帳務系統的即時數字，兩邊會有落差。
- Stage 2 的「套用」是逐批次做局部字串替換，同一批次內同樣的錯字重複出現兩次以上，目前只會替換第一次出現的位置。
- 本機跑的 backend 跟 Render 連的是同一個 Neon 資料庫，而任何一邊啟動時都會把 DB 裡 `pending`/`running` 的工作標成 `interrupted`（`db_utils/job_store.py` 假設只有單一實例）。本機啟動前要先確認沒有人在跑工作，見架設 SOP「Part A 5. 啟動」。

---

# 架設 SOP / Setup Guide

## Part A. 本機測試

### 1. 確認 Python 版本

需要 Python **3.12**，跟 Render 一致（Render 照 repo 裡的 `.python-version` 決定版本）。

```bash
python3 --version
```

如果不是 3.12，可以用 pyenv 另外裝一份，下一步建虛擬環境時把 `python3` 換成它的完整路徑（例如 `~/.pyenv/versions/3.12.4/bin/python`）:

```bash
pyenv install 3.12
```

### 2. 建立虛擬環境、安裝套件

```bash
cd zh-cn-to-tw-backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. 設定環境變數

```bash
cp .env.example .env
```

打開 `.env`，把 `DATABASE_URL`（Neon Postgres 連線字串）跟
`GEMINI_API_KEY` 填成 Render 的 Environment 分頁裡設定的同一組值——
本機開發、桌面版 App、Render 一律連同一個 Neon 資料庫，不是本機另外
申請一組測試用的。要測登入功能需要另外填 `GOOGLE_CLIENT_ID`。

### 4. 準備至少一個管理員帳號

授權完全走 `users`/`permissions` 兩張資料表，不是環境變數。第一次跑
要自己往 Neon 的 `users` 表塞一筆自己的帳號（`role` 填 1 代表管理員），
之後就能從管理員介面維護其他人。

### 5. 啟動

本機 backend 連的是正式的 Neon 資料庫，**一啟動就會把 DB 裡所有 running 的工作標成 `interrupted`**——如果剛好有人在 Render 上跑工作，他的狀態會被改到。啟動前先確認回傳是 `{"active": 0}`:

```bash
curl https://zh-cn-to-tw-backend.onrender.com/api/jobs/active
```

```bash
python3 app.py
```

會跑在 `http://localhost:5001`。健康檢查（不需要登入）:

```bash
curl http://localhost:5001/api/health
```

### 6. 讓桌面版 App 連本機 backend

桌面殼會讀環境變數 `WEB_API_BASE_OVERRIDE`（見 `zh-cn-to-tw-mac` 的 `ContentView.swift`），有設就連那個網址、不連 Render。先把已經開著的 App 關掉，再從 terminal 直接執行 .app 裡的執行檔:

```bash
WEB_API_BASE_OVERRIDE=http://localhost:5001 "/Applications/繁化助手.app/Contents/MacOS/ZhCnToTw"
```

.app 的路徑換成你要測的那一包（例如剛打包好、還在 `zh-cn-to-tw-mac/.build/` 裡的）。這樣登入（系統瀏覽器拿到的 Google token 會送到本機 backend 驗證）、Stage 1、Stage 2 都會走本機的程式碼，不用先部署。

## Part B. 部署到 Render

1. 把這個 repo push 到 GitHub。
2. 到 [render.com](https://render.com) 建立 Web Service，連接這個 repo。
3. **Build Command**: `pip install -r requirements.txt`
4. **Start Command**: `python3 app.py`（這支不是用 gunicorn，`app.py` 本身就是啟動入口）
5. 到 Environment 分頁，把 `.env.example` 列出的變數都設定好。Python 版本由 repo 裡的 `.python-version` 決定（目前 3.12）;如果 Environment 分頁設了 `PYTHON_VERSION`，它會蓋過 `.python-version`，不要另外設。
6. 部署完成後測試 `/api/health`。

> Render 免費方案閒置約 15 分鐘會休眠，喚醒可能要數十秒。`zh-cn-to-tw-web` 的前端會自己做 keep-alive（每 5 分鐘打一次 `/api/health`），不依賴外部排程器。

### 部署前確認沒有工作正在跑

```bash
curl https://<你的部署網址>/api/jobs/active
```

回傳 `{"active": 0}` 才適合部署（重啟會讓正在跑的 job 直接消失，雖然
會被標記成 `interrupted`、使用者可以選擇重試，但體驗上還是有中斷）。

## Part C. 環境變數總覽

見 `.env.example`，裡面每個變數都有註解說明是否必填、用途。重點:

| 變數 | 必填? | 說明 |
|---|---|---|
| `DATABASE_URL` | **必填** | Neon Postgres 連線字串。**Neon project 的區域必須跟 Render service 同區**（目前兩邊都是 `ap-southeast-1` 新加坡），見下面「資料庫與 Render 必須同區」 |
| `GEMINI_API_KEY` | Stage 1 必填 | Google AI Studio 申請 |
| `GOOGLE_CLIENT_ID` | 登入功能必填 | Google Cloud Console 的 OAuth Client ID |
| `ANTHROPIC_API_KEY` | 選填 | 不填就無法選 Claude Haiku 校對 |

## Part D. API 一覽

除了 `GET /api/health`（keep-alive 用，公開）跟 `GET /auth/status`
（查登入狀態本身，未授權不算失敗）跟 `GET /api/ta-notice`（叮嚀內容，
公開）跟 `GET /api/version_check`（強制更新檢查，公開）以外，全部都要
帶 `Authorization: Bearer <session token>`；`/admin/*` 另外要求角色是
管理員。

### 版本檢查

- `GET /api/version_check?os=macos&os_version=11%2B&major=1&minor=3` — 桌面殼在開始 Stage 1/2 前各打一次，回傳是否要強制更新。`os_version` 是分流參數，不是「使用者系統版本」，而是「這包桌面殼本身屬於哪一個 build」——同一個 `os` 之後可能同時發好幾包各自獨立版控的桌面 App（例如 macOS 11 以上一包、10.15 改用 Vision framework 做 OCR 另一包各自一包），省略時預設 `11+`

### 桌面版強制更新門檻

`app_versions` 這張表的唯一鍵是 `(os, os_version)`，不是單獨的 `os`——這是為了讓同一個作業系統底下能同時放好幾筆各自獨立管理版控的門檻。調整門檻或新增一個分流都是直接在 Neon 對這張表下 SQL，不會重跑程式碼裡的種子資料段落（那段是一次性的舊 schema 遷移邏輯，已經在正式環境跑過，見 `db_utils/schema.py` 的 `_ensure_app_versions` 說明）。

### Stage 1

- `POST /api/jobs/from-ocr-text` — Stage 1 唯一入口:送出桌面版在本機 OCR 完的文字
- `GET /api/jobs/<id>` — 查詢進度
- `GET /api/jobs/<id>/download?format=txt|docx` — 下載結果
- `POST /api/jobs/<id>/finalize` — 工作被中斷時，保留部分成果結案

### Stage 2

- `POST /api/jobs/<job_id>/review` — 對已完成的 Stage 1 job 開始校對
- `POST /api/jobs/direct-upload` — 直接上傳已是繁體的 .docx/.txt，跳過 Stage 1
- `GET /api/reviews/<id>` — 查詢進度與 findings
- `POST /api/reviews/<id>/apply` — 套用勾選的建議
- `POST /api/reviews/<id>/rerun` — 重新校對一輪

### 管理

- `GET /api/jobs/active` — 目前有幾個工作在跑（部署前檢查用，公開不需登入）
- `GET /admin/users`、`/admin/permissions`、`/admin/projects` 等 — 管理員介面

---

# English

## Script Murder Mystery Traditionalization Assistant — Backend API

This is the backend API for a tool that converts Simplified-Chinese murder-mystery game scripts into Traditional Chinese. This document is split into two independent parts:

- **[Project Report](#project-report)**: what this system is and why it's built this way.
- **[Setup Guide](#setup-guide)**: step-by-step instructions to get it running.

## Project Report

### What This Is

Game-master / scripts owner often get MMG script in Simplified-Chinese PDFs and need converting to the Traditional Chinese used in Taiwan before players can use them. This tool automates "OCR → Simplified-to-Traditional conversion → LLM polish → human proofreading" into two stages:

- **Stage 1 (Convert)**: upload a PDF → OCR + conversion + LLM polish (sentence breaks, punctuation, fixing obvious OCR misreads) → downloadable .txt/.docx.
- **Stage 2 (Proofread)**: run another LLM pass over Stage 1's output (or a directly-uploaded already-Traditional file) to catch wording/typo/punctuation issues, presented as a structured checklist the user selectively applies.

Human review is still required, but this tool removes most of the manual prep work.

### System Architecture

The project spans four independent repos, wired together via a git meta-repo (`zh-cn-to-tw`) using submodules (see the top-level README):

```
zh-cn-to-tw/                     ← meta-repo, unified local-dev entry, never deployed itself
├── zh-cn-to-tw-backend/         ← this repo, deployed to Render (Flask)
├── zh-cn-to-tw-web/             ← frontend; its main branch is embedded in the desktop app; GitHub Pages serves a separate update-page branch (a pure placeholder)
├── zh-cn-to-tw-mac/             ← macOS desktop shell (Swift/SwiftUI + WKWebView)
└── zh-cn-to-tw-ocr-service/     ← local-only OCR service, launched as a subprocess by the desktop shell
```

This architecture **evolved, it wasn't designed this way from day one** — the original version was simple: Render handled everything (including OCR), and a browser opened GitHub Pages directly. It turned out Render's free tier (0.1 CPU, 512MB RAM) couldn't handle PaddleOCR at all (see "Why OCR Moved to the User's Own Machine" below), which is what eventually produced the desktop app. The desktop path is now **the only way to be used**:

- **Desktop path** (zh-cn-to-tw-mac): OCR runs on the user's own machine (`zh-cn-to-tw-ocr-service` on 11+, Apple's Vision framework on 10.15); only the already-OCR'd plain text is sent to this backend for conversion/LLM polish (`POST /api/jobs/from-ocr-text`). This backend never touches a PDF.
- **The old browser path has been removed** (2026-10-03): `POST /api/jobs` — PDFs uploaded straight to this backend with OCR running on Render — is gone, together with `run_ocr_stage`, `ocr_utils/`, and the paddle-related packages. See "Removing Server-Side OCR and Moving to Python 3.12" below for why. In browser mode, the frontend's Stage 1 now just tells the user to use the desktop app.

### Why OCR Moved to the User's Own Machine (major architecture pivot)

This was the biggest turn the project took, and it took several failed attempts to get there:

1. **PP-OCRv4 crashed with SIGILL on Render** — the specific host CPU didn't support a vectorized instruction the model version used. Switched to PP-OCRv3 to work around it.
2. **PaddleOCR's very first inference call spiked RSS to 2.6–2.8GB**, far past Render free tier's 512MB ceiling, getting OOM-killed.
3. **CPU quota exhaustion got treated as unresponsiveness** — the free tier is only 0.1 CPU, and OCR is CPU-hungry enough to routinely push a single request past the platform's timeout judgment.
4. Upgrading to a paid tier wasn't realistic for a low-margin, personal-scale project.

Mitigations tried (all just delayed the problem, none fixed it): pinning PaddleOCR to a single CPU thread, streaming pages one at a time instead of a single memory spike, lowering DPI. The **real fix was moving OCR off Render entirely** — the desktop version offloads it to the user's own machine (`zh-cn-to-tw-ocr-service`), leaving the backend with only what genuinely must stay remote: the database, LLM API keys, login, the admin interface, and job/review state.

**A considered-then-abandoned alternative**: bundling the entire backend into the desktop `.app` too (`packaging/backend_service.spec`, since deleted), running a full local copy. This was abandoned because a locally-run backend serving the web UI over HTTP binds to a randomly-assigned port on each launch (deliberately, to avoid a stale process squatting a fixed port) — and `localStorage` keys origin by `scheme+host+port`. A different port every launch means every launch is effectively a new origin, so the login session stored in `localStorage` could never survive a restart — the user had to log in again every single time. The fix was to have the desktop shell load the web UI from a fixed `file://` path instead (no local HTTP server involved at all), giving it a stable origin. Credentials (the DB connection string, LLM API keys) never ship inside the desktop app at all — they stay on Render exclusively. This was a deliberate security tradeoff: client-side secrets can't actually be protected (the code must be able to decrypt them to use them, so encryption only raises the bar, it doesn't close the door) — so the desktop app is simply never handed anything worth stealing; it only gets a scoped, revocable session token.

### Removing Server-Side OCR and Moving to Python 3.12 (2026-10-03)

After OCR moved to the desktop app, the server-side path (`POST /api/jobs` → `run_ocr_stage` → PaddleOCR) was never torn down. In practice nobody reached it any more, but it still carried two real costs:

1. **Every deploy installed paddle**: paddlepaddle, paddleocr, PyMuPDF, and Pillow in `requirements.txt` existed only for this path.
2. **It pinned Python to 3.9**: `.python-version` said 3.9 because paddlepaddle 2.6.2 has no wheels for newer Pythons (commit `7e2a1bc`). That in turn froze `google-genai` at 0.3.0 (never upgraded since the first commit) — google-genai requires Python 3.10+ from 1.48.0 on, so 3.9 tops out at 1.47.0, and 1.47.0 has no `thinking_level` setting for the Gemini 3 family.

What triggered the cleanup was Gemini 3.6 Flash repeatedly returning `503 UNAVAILABLE` ("This model is currently experiencing high demand"): in the same window Flash Lite answered instantly, while 3.6 Flash failed all 9 calls across 3 jobs. Comparing directly against the REST API: a one-line prompt succeeded; the real prompt (~4,200 characters) at the default thinking level got 503; sending only half of it still got 503; setting `thinkingLevel` to `low` succeeded — so the deciding factor looked like thinking level, not length (the `low` case was only tested once, a tiny sample). Setting the thinking level from code requires the newer SDK, which requires dropping paddle first. The thinking level is **deliberately left unchanged** for now and still uses the model default; the SDK supports changing it whenever that's wanted.

Version changes that came along with the upgrade:

- `google-genai` 0.3.0 → 2.27.0.
- `google-auth` 2.32.0 → 2.59.1: the new google-genai requires `google-auth>=2.56`. This package also does Google sign-in verification (`verify_oauth2_token` in `auth_utils/auth.py`); after the upgrade, sign-in was verified with a real account.
- `cryptography` pinned at 48.0.1: a dependency pulled in by the new google-auth. From 49.0 on there is no macOS Intel (x86_64) wheel, so on an Intel Mac pip falls back to building from source (which needs Rust) and hangs; 48.0.1 is the last release with wheels for both macOS Intel and Linux, and pinning it keeps local and Render on the same version.

Pre-release verification touched neither the production DB nor a deploy: a fresh 3.12 venv with `DATABASE_URL` pointed at an unreachable address, calling the routes and the Stage 1/2 flows directly against real LLMs (Flash Lite, Haiku, 3.6 Flash's 503 correctly classified as a transient error, Stage 2's JSON mode, a fake token being rejected); then `WEB_API_BASE_OVERRIDE` to point the desktop app at the local backend, verifying sign-in and Stage 1 with a real account (see Setup Guide "Part A 6. Point the Desktop App at the Local Backend").

### The Database Must Sit in the Same Region as Render (a major performance problem found only on 2026-08-25)

**The Neon project's region must match the Render service's region.** Both are currently AWS `ap-southeast-1` (Singapore). This is not an optimization tip — it is the single biggest determinant of this system's responsiveness.

Render ran in Singapore while Neon had been created in `us-east-2` (Ohio), so every database connection crossed the Pacific six times. Measured: `/api/health`, which touches no database, answered in 0.11 s, while `/api/jobs/active` — one `COUNT`, no authentication — took 1.76 s, and **repeating the call never made it faster** (ruling out cold starts). More glaring still: the developer's home broadband in Taiwan reached Ohio faster (199.7 ms) than Render's datacenter reached its own database (~236 ms) — **a datacenter losing to a residential connection is itself the signal that something is topologically wrong**.

What exposed it was a ratio. A raw TCP handshake measured with `socket` (`SYN→SYN/ACK` is exactly one round trip by definition) came to 199.7 ms, while opening one psycopg2 connection took 1228 ms: `1228 ÷ 199.7 = 6.15`, matching the expected round-trip count for TCP (1) + TLS (1–2) + SCRAM (~3). **A quotient landing on a small integer means the operation contains essentially no computation — it is pure network round trips**, and round trips are priced by distance. The same ratio also ruled out the competing hypothesis that Render's 0.1 CPU was throttling the crypto-heavy handshake, since that would have pushed the quotient far above 6.

After moving the database to Singapore (with `/api/health` subtracted as the baseline):

| | Before | After |
|---|---|---|
| Net database cost | 1.76 s | **0.03 s** |
| `/api/jobs/active` | 1.78–1.99 s | **0.12–0.21 s** |

Roughly **59×**. The new database independently reproduced the same ratio (`connect ÷ RTT = 5.98`); two entirely different network paths both landing on 6 makes this a repeatable diagnostic rather than a coincidence.

**An existing Neon project's region cannot be changed** (stated explicitly in their docs) — you must create a project in the target region and migrate the data. The migration itself was straightforward: the whole database is 9.6 MB across 1606 rows, and **no client holds database credentials** (the web frontend, Mac, and Windows shells all go through the Render API), so the only things needing an update were Render's environment variable and the local `.env`.

Because distance is no longer the bottleneck, two previously-scoped optimizations were **deliberately not done**: a psycopg3 connection pool (worth only ~130 ms more per request once co-located, while introducing a new failure mode — Neon suspends after 5 minutes idle and severs pooled connections), and cutting the 5 connections opened per authenticated request down to 2 (150 ms → 60 ms, imperceptible to users). The full reasoning lives in `db_utils/connection.py`'s fourth historical lesson.

### File Layout

```
app.py                  routing entry point
configs/config.py       centralized settings & env vars
db_utils/                database layer
  ├── connection.py       connection management (see the module docstring)
  ├── schema.py            table creation/migration
  ├── job_store.py         job/review state persisted to Neon
  └── app_versions.py      forced-update threshold lookups for the desktop app
auth_utils/               Google sign-in verification, sessions, whitelist
jobs/ review/             Stage 1/Stage 2 job management
pipeline/orchestrator.py  Stage 1's conversion→polish orchestration (no OCR; OCR always runs on the user's machine)
llm_utils/                Gemini/Claude call wrappers
usage/                    usage tracking (Gemini call counts, Claude tokens/cost)
admin_utils/              admin interface (users/permissions/projects)
output_utils/             .txt/.docx export
convert_utils/            OpenCC Simplified→Traditional conversion
validators/               output validation (e.g. leftover-Simplified-character checks)
```

### Known Limitations

- `usage_log` (the Neon table) counts "how many times this tool called the API," not a live read of Google's/Anthropic's own billing systems — the two will diverge.
- Stage 2's "apply" does batch-scoped local string replacement; if the same typo appears more than once within one batch, only the first occurrence gets replaced.
- A locally-run backend connects to the same Neon database as Render, and either one marks every `pending`/`running` job in the DB as `interrupted` on startup (`db_utils/job_store.py` assumes a single instance). Before starting one locally, confirm nobody has a job running — see Setup Guide "Part A 5. Start It".

---

# Setup Guide

## Part A. Local Testing

### 1. Confirm Your Python Version

Python **3.12** is required, matching Render (Render picks the version from the repo's `.python-version`).

```bash
python3 --version
```

If it isn't 3.12, install one with pyenv, and in the next step replace `python3` with its full path (e.g. `~/.pyenv/versions/3.12.4/bin/python`):

```bash
pyenv install 3.12
```

### 2. Create a Virtual Environment, Install Packages

```bash
cd zh-cn-to-tw-backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Set Environment Variables

```bash
cp .env.example .env
```

Open `.env` and set `DATABASE_URL` (a Neon Postgres connection string)
and `GEMINI_API_KEY` to the same values already configured on Render's
Environment tab — local dev, the desktop app, and Render all connect
to the same Neon database, not a separate one set up just for local
testing. Testing login also requires `GOOGLE_CLIENT_ID`.

### 4. Seed at Least One Admin Account

Authorization is entirely table-driven (`users`/`permissions`), not
environment-variable-driven. On first run, manually insert your own
account into Neon's `users` table (set `role` to 1 for admin); after
that, manage everyone else from the admin UI.

### 5. Start It

The local backend connects to the production Neon database and **marks every running job in the DB as `interrupted` the moment it starts** — if someone happens to have a job running on Render, its state gets changed. Before starting, confirm this returns `{"active": 0}`:

```bash
curl https://zh-cn-to-tw-backend.onrender.com/api/jobs/active
```

```bash
python3 app.py
```

Runs on `http://localhost:5001`. Health check (no auth needed):

```bash
curl http://localhost:5001/api/health
```

### 6. Point the Desktop App at the Local Backend

The desktop shell reads the `WEB_API_BASE_OVERRIDE` environment variable (see `ContentView.swift` in `zh-cn-to-tw-mac`); when set, it talks to that URL instead of Render. Quit any running copy of the app first, then run the executable inside the .app directly from a terminal:

```bash
WEB_API_BASE_OVERRIDE=http://localhost:5001 "/Applications/繁化助手.app/Contents/MacOS/ZhCnToTw"
```

Replace the .app path with whichever build you're testing (e.g. a fresh one still sitting in `zh-cn-to-tw-mac/.build/`). Sign-in (the Google token the system browser obtains gets verified by the local backend), Stage 1, and Stage 2 then all run the local code, with no deploy needed.

## Part B. Deploy to Render

1. Push this repo to GitHub.
2. Create a Web Service on [render.com](https://render.com), connect this repo.
3. **Build Command**: `pip install -r requirements.txt`
4. **Start Command**: `python3 app.py` (not gunicorn — `app.py` is the entry point itself)
5. On the Environment tab, set every variable listed in `.env.example`. The Python version comes from the repo's `.python-version` (currently 3.12); a `PYTHON_VERSION` variable on the Environment tab would override it, so don't set one.
6. After deploy, test `/api/health`.

> Render's free tier sleeps after ~15 minutes idle; waking up can take tens of seconds. `zh-cn-to-tw-web`'s frontend self-heartbeats (pinging `/api/health` every 5 minutes) instead of relying on any external scheduler.

### Before Deploying, Confirm Nothing Is Running

```bash
curl https://<your-deployed-url>/api/jobs/active
```

Only deploy when this returns `{"active": 0}` — a restart makes any
in-flight job disappear (it gets marked `interrupted` and the user can
choose to retry, but it's still a real interruption).

## Part C. Environment Variables Overview

See `.env.example` — every variable there has an inline comment on
whether it's required and what it's for. The essentials:

| Variable | Required? | Description |
|---|---|---|
| `DATABASE_URL` | **Required** | Neon Postgres connection string. **The Neon project's region must match the Render service's region** (both are currently `ap-southeast-1`, Singapore) — see "The Database Must Sit in the Same Region as Render" above |
| `GEMINI_API_KEY` | Required for Stage 1 | From Google AI Studio |
| `GOOGLE_CLIENT_ID` | Required for login | OAuth Client ID from Google Cloud Console |
| `ANTHROPIC_API_KEY` | Optional | Without it, Claude Haiku proofreading is unavailable |

## Part D. API Overview

Aside from `GET /api/health` (keep-alive, public), `GET /auth/status`
(checks login status itself, unauthorized isn't a failure),
`GET /api/ta-notice` (the notes content, public), and
`GET /api/version_check` (forced-update check, public), every endpoint
requires `Authorization: Bearer <session token>`; `/admin/*` further
requires the admin role.

### Version Check

- `GET /api/version_check?os=macos&os_version=11%2B&major=1&minor=3` — the desktop shell calls this once before starting Stage 1 and once before Stage 2, to find out whether it must force an update. `os_version` isn't "the user's OS version" — it's a build-tier selector: which desktop build this shell actually is, since the same `os` may end up with several independently-versioned desktop builds shipping side by side (e.g. one for macOS 11+, one for 10.15 that does Stage 1 OCR natively via Vision framework). Omitting it defaults to `11+`

### Desktop Forced-Update Thresholds

The `app_versions` table's unique key is `(os, os_version)`, not `os` alone — this lets the same OS have several independently-managed threshold rows at once. Adjusting a threshold, or adding a new tier, is done with direct SQL against the table on Neon; it never re-runs the seed-data code path (that's one-time legacy-schema migration logic that already ran in production — see `_ensure_app_versions` in `db_utils/schema.py`).

### Stage 1

- `POST /api/jobs/from-ocr-text` — Stage 1's only entry point: submits text the desktop app already OCR'd locally
- `GET /api/jobs/<id>` — check progress
- `GET /api/jobs/<id>/download?format=txt|docx` — download the result
- `POST /api/jobs/<id>/finalize` — close out an interrupted job, keeping partial results

### Stage 2

- `POST /api/jobs/<job_id>/review` — start proofreading a finished Stage 1 job
- `POST /api/jobs/direct-upload` — upload an already-Traditional .docx/.txt, skipping Stage 1
- `GET /api/reviews/<id>` — check progress and findings
- `POST /api/reviews/<id>/apply` — apply the selected suggestions
- `POST /api/reviews/<id>/rerun` — run another proofreading pass

### Admin

- `GET /api/jobs/active` — how many jobs are currently running (for pre-deploy checks, no auth required)
- `GET /admin/users`, `/admin/permissions`, `/admin/projects`, etc. — admin interface
