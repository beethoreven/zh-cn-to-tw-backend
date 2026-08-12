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

- **桌面版**(zh-cn-to-tw-mac):OCR 在使用者自己的機器上跑(`zh-cn-to-tw-ocr-service`)，只把 OCR 完的簡體文字傳給這支 backend 做簡轉繁/LLM 潤飾(`POST /api/jobs/from-ocr-text`)，backend 完全不碰 PDF 也不跑 PaddleOCR。
- **舊的瀏覽器版路徑**（PDF 直接上傳給這支 backend、OCR 也在 Render 上跑，`pipeline/orchestrator.py` 的 `run_ocr_stage`）:程式碼還在，`POST /api/jobs` 這支 endpoint 沒有被刪掉。**但這支 endpoint 有掛 `require_auth`**（見 `app.py`）——沒有合法 session token、或帳號不在白名單裡一律 401，陌生人隨便找到網址亂打是打不動任何東西的。`zh-cn-to-tw-web` 的 `main` 分支為放實際網頁內容的地方，從結構上就不在 GitHub Pages 的服務範圍內，GitHub Pages 服務的是另一個完全獨立、沒有共同檔案的 `update-page` 分支，只有一個「網站建構中」的佔位頁(見 `zh-cn-to-tw-web` README「為什麼這個 repo 的內容不會出現在 GitHub Pages 上」)。這支 backend 的程式碼本身還沒有跟進拆掉這條路徑，實際能碰到它的只剩本機開發環境，或白名單使用者自己手動組請求。

### 為什麼 OCR 搬到使用者本機（重大架構決策）

這是整個專案最大的一次轉向，過程踩了不少坑:

1. **PP-OCRv4 在 Render 上直接 SIGILL 崩潰**——特定主機的 CPU 不支援這個模型版本用到的向量化指令。改用 PP-OCRv3 繞過。
2. **PaddleOCR 光是第一次推論就把 RSS 衝到 2.6-2.8GB**，遠超過 Render 免費方案的 512MB 上限，OOM 被系統強制關閉。
3. **CPU 配額耗盡被判定無回應**——免費方案只有 0.1 CPU，OCR 這種吃 CPU 的工作很容易把單一請求的處理時間拖到平台判定逾時。
4. 升級付費方案在這個低毛利、個人使用規模的專案上不現實。

嘗試過的緩解方式（都只是延後問題，沒有根治）:限制 PaddleOCR 只用單一 CPU 執行緒、改成逐頁串流處理避免一次性記憶體尖峰、調降 DPI。這些對瀏覽器版現在還在用，但**真正的解法是把 OCR 整個搬離 Render**——桌面版把這一段丟到使用者自己的機器上執行（`zh-cn-to-tw-ocr-service`），backend 只保留 DB、LLM API 金鑰、登入、管理員介面、job/review 狀態這些「一定要連外」的部分。

**曾經考慮過、後來放棄的方案**:讓桌面版把整支 backend 也一起打包進 .app（`packaging/backend_service.spec`，已刪除），本機自己跑一份完整的 backend。放棄原因是本機 backend 用 HTTP 供應網頁時，監聽的 port 是每次啟動隨機配的（刻意避免舊 process 卡住固定 port），而 `localStorage` 是照 `scheme+host+port` 算 origin 的——port 每次不一樣，等於使用者每次開 App 登入的 session 都救不回來，變成每次啟動都要重新登入。最後改成桌面殼直接用固定的 `file://` 路徑內嵌網頁（不透過任何本機 HTTP 伺服器），origin 因此穩定；憑證（DB 連線字串、LLM API 金鑰）完全不進桌面版 App，一律留在 Render——這是刻意的安全取捨:客戶端的機密本質上無法真正保護（程式執行時必須能解密才能用，加密只是提高門檻），所以憑證乾脆不下發，桌面版只拿一個範圍有限、可撤銷的 session token。

### 資料庫層的三次教訓

這個系統的資料庫連線層曾經在同一天內連續踩過三個層層遞進的坑，值得完整記錄:

1. **全域鎖拖垮全站**——早期為了「安全」，用一把 Python `threading.Lock` 序列化所有資料庫存取，而取得連線的實作是每次都對 Neon 開一條全新連線（實測 1.2-1.6 秒）且沒有 timeout。這把鎖從加 Postgres 支援那天就在，不是後來哪次修改引入的——Postgres 本身透過 MVCC 正確處理並行，這把鎖從來就不是必要的。前端輪詢疊加高頻請求後，一次連線異常就會鎖住全站達分鐘等級，且 Neon 端完全看不到連線進來（請求根本沒送到，卡在自己 process 內部搶鎖）。
2. **改用連線池，結果重演同一個問題**——第一直覺是「加連線池減少重建連線的成本」，但 `psycopg2.pool.ThreadedConnectionPool` 內部用同一把鎖同時保護「借連線」跟「建立新連線」，池子需要生出新連線時，`psycopg2.connect()`（真正的網路操作）是在持有那把鎖的狀態下執行的——一次新連線卡住，其他所有執行緒連「還一條已經用完的連線」都做不到。實測 10 個併發請求，第一個完成後其餘 9 個卡了 90 秒以上動不了。這正是要修的問題本身，只是換了一層、藏得更隱密，最後整個撤回。
3. **拿掉鎖之後完全沒上限，把 instance 打掛**——移除全域鎖解決了凍結，但那把鎖同時也是唯一在做反壓的東西，拿掉後對正式服務打 10 個併發請求直接把 Render instance（0.1 CPU/512MB）打成 502。最終方案:每次呼叫獨立開一條連線、用完即關（不共用池），但用 `threading.Semaphore` 把同時存在的連線數限制在一個安全上限內——號誌跟互斥鎖的關鍵差別是它允許 N 個同時進行，一條連線卡住只用掉 N 個名額中的一個，其他人不受影響；再加上 `connect_timeout`，任何一條連線最多卡固定秒數就會快速失敗釋出名額，最壞情況因此有明確邊界。

完整的技術細節、每一步的實測數據，見 `db_utils/connection.py` 檔案開頭的說明。

### 用量統計時區的一次修正

Gemini 每日用量重置時間點，官方文件字面上寫「美西午夜重置」，程式一開始也是照這個做的。但使用者實測發現對不起來——台灣時間下午某個時刻額度已經重置，換算成美西時間當下卻還沒到午夜。改成用 **UTC 午夜**當界線後，跟實測結果吻合（Google 官方支援論壇上也有其他人回報美西午夜之後沒有準時重置的狀況）。這是一個「文件寫的跟實測不符時，該相信實測」的例子。

### 登入與 Session 架構

主持人／管理員的身分驗證用 Google Identity Services 取得 ID Token，但**不會**把這個 Token 直接當長效憑證使用——Google ID Token 效期約 1 小時，直接拿來當每支 API 都要帶的憑證，會導致長時間的 Stage 1/校對作業（尤其遇到額度限制重試時）中途被登出，算好的結果因為登入過期存不下來。改成應用程式自己簽發、管理生命週期的 session token（`auth_utils/sessions.py`）:Google ID Token 只在 `/auth/login` 驗證一次，換發一個不透明字串（不是 JWT）存進 `sessions` 表，效期用「滑動視窗」設計（每次使用都刷新，只要使用者還在用就不會過期）。撤銷（管理員停用帳號）走另一條獨立路徑——`auth_utils/whitelist.py` 每次請求都即時查 `users`/`permissions` 表（帶 30 秒 TTL 快取），跟 session token 本身是否有效是兩件事，管理員在後台停用一個帳號，30 秒內就會生效，不受 session 效期影響。

### 「阿舍老師的叮嚀」內容供應方式

主介面左側有一塊純文字提示區塊，內容來自 `ta-notice.txt`（這個 repo根目錄）。這個功能的供應方式改了三次:

1. 最早直接放在 `zh-cn-to-tw-web`，前端用 `fetch()` 讀同目錄檔案——瀏覽器版可以，但桌面版用 `file://` 載入頁面後行不通。
2. 嘗試 `XMLHttpRequest`、隱藏 `<iframe>` 讀取——都失敗（`file://` 底下 JS 完全沒有辦法讀取同目錄的另一個檔案，不管透過哪個 API 都一樣，是這個平台的結構性限制）。
3. 最後採用現在這個方案:內容移進這支 backend，透過 `GET /api/ta-notice`（不需要登入）供應，瀏覽器版跟桌面版共用同一條路徑，維護者一樣只要編輯 `ta-notice.txt`、commit、push、部署即可生效，不用碰任何程式碼。

### 檔案結構

```
app.py                  路由入口
configs/config.py       集中管理設定值與環境變數
db_utils/                資料庫層
  ├── connection.py       連線管理（見上方「資料庫層的三次教訓」）
  ├── schema.py            建表/遷移
  └── job_store.py         job/review 狀態持久化到 Neon
auth_utils/               Google 登入驗證、session、白名單
jobs/ review/             Stage 1/Stage 2 各自的工作管理
pipeline/orchestrator.py  Stage 1 的 OCR→簡轉繁→潤飾 流程編排
ocr_utils/                PaddleOCR 包裝（只有瀏覽器版路徑會用到，桌面版繞過）
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
- 瀏覽器版跟桌面版的 OCR 資源風險不對稱（見上方「系統架構」）——瀏覽器版仍然吃 Render 資源限制，桌面版已經完全繞開。

---

# 架設 SOP / Setup Guide

## Part A. 本機測試

### 1. 確認 Python 版本

```bash
python3 --version
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

打開 `.env`，至少要填 `DATABASE_URL`（Neon Postgres 連線字串）跟
`GEMINI_API_KEY` 才能跑 Stage 1；要測登入功能需要另外填
`GOOGLE_CLIENT_ID`。

> 這個專案沒有 SQLite 退回機制——`DATABASE_URL` 沒填，所有需要資料庫的操作都會直接報錯，不會靜默退回本機檔案。本機開發、桌面版 App、Render 一律連同一個 Neon 資料庫，只有一種資料來源。

### 4. 準備至少一個管理員帳號

授權完全走 `users`/`permissions` 兩張資料表，不是環境變數。第一次跑
要自己往 Neon 的 `users` 表塞一筆自己的帳號（`role` 填 1 代表管理員），
之後就能從管理員介面維護其他人。

### 5. 啟動

```bash
python3 app.py
```

會跑在 `http://localhost:5001`。健康檢查（不需要登入）:

```bash
curl http://localhost:5001/api/health
```

## Part B. 部署到 Render

1. 把這個 repo push 到 GitHub。
2. 到 [render.com](https://render.com) 建立 Web Service，連接這個 repo。
3. **Build Command**: `pip install -r requirements.txt`
4. **Start Command**: `python3 app.py`（這支不是用 gunicorn，`app.py` 本身就是啟動入口）
5. 到 Environment 分頁，把 `.env.example` 列出的變數都設定好。
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
| `DATABASE_URL` | **必填** | Neon Postgres 連線字串 |
| `GEMINI_API_KEY` | Stage 1 必填 | Google AI Studio 申請 |
| `GOOGLE_CLIENT_ID` | 登入功能必填 | Google Cloud Console 的 OAuth Client ID |
| `ANTHROPIC_API_KEY` | 選填 | 不填就無法選 Claude Haiku 校對 |

## Part D. API 一覽

除了 `GET /api/health`（keep-alive 用，公開）跟 `GET /auth/status`
（查登入狀態本身，未授權不算失敗）跟 `GET /api/ta-notice`（叮嚀內容，
公開）以外，全部都要帶 `Authorization: Bearer <session token>`；
`/admin/*` 另外要求角色是管理員。

### Stage 1

- `POST /api/jobs` — 上傳 PDF（瀏覽器版路徑，OCR 在 Render 上跑）
- `POST /api/jobs/from-ocr-text` — 直接送已經 OCR 完的文字（桌面版路徑）
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

- **Desktop path** (zh-cn-to-tw-mac): OCR runs on the user's own machine (`zh-cn-to-tw-ocr-service`); only the already-OCR'd plain text is sent to this backend for conversion/LLM polish (`POST /api/jobs/from-ocr-text`). This backend never touches a PDF or runs PaddleOCR at all for this path.
- **Old browser path** (the PDF uploaded straight to this backend, OCR also running on Render via `pipeline/orchestrator.py`'s `run_ocr_stage`): the code is still here — `POST /api/jobs` hasn't been removed. **But this endpoint is gated by `require_auth`** (see `app.py`) — without a valid session token, or with an email outside the whitelist, it's an automatic 401; a stranger who stumbles on the URL and hits it can't trigger anything. `zh-cn-to-tw-web`'s `main` branch, where the real web content lives, is structurally outside anything GitHub Pages serves; Pages serves a completely separate `update-page` branch with no files in common, holding only a "site under construction" placeholder (see `zh-cn-to-tw-web`'s README, "Why This Repo's Content Never Appears on GitHub Pages"). This backend's own code hasn't caught up to removing the path yet — what can still reach it is local dev, or a whitelisted user manually crafting a request.

### Why OCR Moved to the User's Own Machine (major architecture pivot)

This was the biggest turn the project took, and it took several failed attempts to get there:

1. **PP-OCRv4 crashed with SIGILL on Render** — the specific host CPU didn't support a vectorized instruction the model version used. Switched to PP-OCRv3 to work around it.
2. **PaddleOCR's very first inference call spiked RSS to 2.6–2.8GB**, far past Render free tier's 512MB ceiling, getting OOM-killed.
3. **CPU quota exhaustion got treated as unresponsiveness** — the free tier is only 0.1 CPU, and OCR is CPU-hungry enough to routinely push a single request past the platform's timeout judgment.
4. Upgrading to a paid tier wasn't realistic for a low-margin, personal-scale project.

Mitigations tried (all just delayed the problem, none fixed it): pinning PaddleOCR to a single CPU thread, streaming pages one at a time instead of a single memory spike, lowering DPI. These are still in place for the browser path, but the **real fix was moving OCR off Render entirely** — the desktop version offloads it to the user's own machine (`zh-cn-to-tw-ocr-service`), leaving the backend with only what genuinely must stay remote: the database, LLM API keys, login, the admin interface, and job/review state.

**A considered-then-abandoned alternative**: bundling the entire backend into the desktop `.app` too (`packaging/backend_service.spec`, since deleted), running a full local copy. This was abandoned because a locally-run backend serving the web UI over HTTP binds to a randomly-assigned port on each launch (deliberately, to avoid a stale process squatting a fixed port) — and `localStorage` keys origin by `scheme+host+port`. A different port every launch means every launch is effectively a new origin, so the login session stored in `localStorage` could never survive a restart — the user had to log in again every single time. The fix was to have the desktop shell load the web UI from a fixed `file://` path instead (no local HTTP server involved at all), giving it a stable origin. Credentials (the DB connection string, LLM API keys) never ship inside the desktop app at all — they stay on Render exclusively. This was a deliberate security tradeoff: client-side secrets can't actually be protected (the code must be able to decrypt them to use them, so encryption only raises the bar, it doesn't close the door) — so the desktop app is simply never handed anything worth stealing; it only gets a scoped, revocable session token.

### Three Lessons at the Database Layer

The database connection layer went through three progressively deeper lessons in a single day, worth recording in full:

1. **A global lock froze the whole service** — early on, a Python `threading.Lock` serialized every database access "for safety," and acquiring a connection meant opening a brand-new one to Neon every time (measured 1.2–1.6s), with no timeout. This lock had been there since the commit that first added Postgres support — it wasn't introduced by any later change. Postgres itself handles concurrency correctly via MVCC; the lock was never actually necessary. Once frontend polling piled on enough request volume, a single slow connection could freeze the entire service for minutes, and Neon would show zero incoming connections the whole time — the requests never even left the process, stuck queued behind the lock.
2. **Switching to a connection pool reproduced the exact same bug** — the first instinct was "add a pool to amortize the reconnect cost," but `psycopg2.pool.ThreadedConnectionPool` guards both "borrow a connection" and "create a new connection" with the same internal lock — when the pool needs to grow, `psycopg2.connect()` (the actual network call) runs *while holding that lock*. One stuck new-connection attempt meant every other thread — including ones just trying to *return* a connection they'd already finished with — was blocked too. Measured: 10 concurrent requests, the first one finished, and the other 9 sat stuck for 90+ seconds. This was the exact bug being fixed, just relocated one layer deeper and harder to see. Abandoned entirely.
3. **Removing the lock with no replacement took down the instance** — removing the global lock fixed the freeze, but that lock had also been the only thing providing backpressure. With it gone, ten concurrent requests against the live Render instance (0.1 CPU/512MB) crashed it outright with 502s. The final design: every call opens and closes its own independent connection (no shared pool), bounded by a `threading.Semaphore` capping how many can exist at once. The key difference from a mutex is that a semaphore admits N concurrently — one stuck connection consumes one of N slots, everyone else proceeds unaffected — paired with a `connect_timeout` so any single connection fails fast rather than hanging indefinitely, giving the worst case a real ceiling.

Full technical detail and the actual measurements are in the module docstring at the top of `db_utils/connection.py`.

### A Timezone Correction for Usage Stats

Gemini's daily quota reset time is documented as "resets at midnight Pacific time," and the code originally followed that literally. Real-world testing showed it didn't line up — at a given Taiwan-afternoon moment the quota had already reset, while the corresponding Pacific time hadn't reached midnight yet. Switching the boundary to **UTC midnight** matched observed behavior (other users on Google's own support forum have also reported the documented Pacific-midnight reset not firing on time). A case of trusting real measurement over documentation text.

### Login & Session Architecture

GM/admin identity verification uses Google Identity Services to obtain an ID Token, but that token is deliberately **not** used directly as a long-lived credential — Google ID Tokens expire in about an hour, and using one as the bearer credential for every API call would silently sign users out mid-session during long Stage 1/proofreading runs (especially once quota-retry backoff is involved), losing already-computed results that couldn't be saved under an expired credential. Instead, the app mints and manages its own session token (`auth_utils/sessions.py`): the Google ID Token is verified exactly once at `/auth/login`, exchanged for an opaque string (not a JWT) stored in a `sessions` table with a sliding expiry (refreshed on every use — a session that's actively being used effectively never expires). Revocation (an admin deactivating an account) is handled by a fully separate path — `auth_utils/whitelist.py` checks the `users`/`permissions` tables live on every request (with a 30-second TTL cache) — independent of whether the session token itself is still valid, so deactivating someone in the admin panel takes effect within 30 seconds regardless of session state.

### How the "Teacher's Notes" Sidebar Content Is Served

The main page has a plain-text tip panel sourced from `ta-notice.txt` (in this repo's root). How it's served has changed three times:

1. Originally lived in `zh-cn-to-tw-web`, read via `fetch()` on the same-directory file — worked fine in the browser, but broke once the desktop app started loading pages via `file://`.
2. Tried `XMLHttpRequest` and a hidden `<iframe>` — both failed too (under `file://`, JS has no way at all to read another file in the same directory, regardless of which API is used — a structural platform limitation, not an API choice).
3. The final approach: moved into this backend, served via `GET /api/ta-notice` (no login required), shared by both the browser and desktop versions. Maintaining it is still just editing `ta-notice.txt`, commit, push, deploy — no code changes needed.

### File Layout

```
app.py                  routing entry point
configs/config.py       centralized settings & env vars
db_utils/                database layer
  ├── connection.py       connection management (see "Three Lessons" above)
  ├── schema.py            table creation/migration
  └── job_store.py         job/review state persisted to Neon
auth_utils/               Google sign-in verification, sessions, whitelist
jobs/ review/             Stage 1/Stage 2 job management
pipeline/orchestrator.py  Stage 1's OCR→conversion→polish orchestration
ocr_utils/                PaddleOCR wrapper (only the browser path uses this; desktop bypasses it)
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
- OCR resource risk is asymmetric between the two paths (see "System Architecture" above) — the browser path still carries Render's resource constraints; the desktop path has fully sidestepped them.
- `zh-cn-to-tw-mac`/`zh-cn-to-tw-ocr-service` aren't yet git submodules of the top-level meta-repo, and are still local-only (no GitHub remote).

---

# Setup Guide

## Part A. Local Testing

### 1. Confirm Your Python Version

```bash
python3 --version
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

Open `.env` and fill in at minimum `DATABASE_URL` (a Neon Postgres
connection string) and `GEMINI_API_KEY` to run Stage 1; testing login
also requires `GOOGLE_CLIENT_ID`.

> This project has no SQLite fallback — an unset `DATABASE_URL` makes every database operation raise immediately, never silently falling back to a local file. Local dev, the desktop app, and Render all connect to the same Neon database; there is only one source of truth.

### 4. Seed at Least One Admin Account

Authorization is entirely table-driven (`users`/`permissions`), not
environment-variable-driven. On first run, manually insert your own
account into Neon's `users` table (set `role` to 1 for admin); after
that, manage everyone else from the admin UI.

### 5. Start It

```bash
python3 app.py
```

Runs on `http://localhost:5001`. Health check (no auth needed):

```bash
curl http://localhost:5001/api/health
```

## Part B. Deploy to Render

1. Push this repo to GitHub.
2. Create a Web Service on [render.com](https://render.com), connect this repo.
3. **Build Command**: `pip install -r requirements.txt`
4. **Start Command**: `python3 app.py` (not gunicorn — `app.py` is the entry point itself)
5. On the Environment tab, set every variable listed in `.env.example`.
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
| `DATABASE_URL` | **Required** | Neon Postgres connection string |
| `GEMINI_API_KEY` | Required for Stage 1 | From Google AI Studio |
| `GOOGLE_CLIENT_ID` | Required for login | OAuth Client ID from Google Cloud Console |
| `ANTHROPIC_API_KEY` | Optional | Without it, Claude Haiku proofreading is unavailable |

## Part D. API Overview

Aside from `GET /api/health` (keep-alive, public), `GET /auth/status`
(checks login status itself, unauthorized isn't a failure), and
`GET /api/ta-notice` (the notes content, public), every endpoint
requires `Authorization: Bearer <session token>`; `/admin/*` further
requires the admin role.

### Stage 1

- `POST /api/jobs` — upload a PDF (browser path, OCR runs on Render)
- `POST /api/jobs/from-ocr-text` — submit already-OCR'd text (desktop path)
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
