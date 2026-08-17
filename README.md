# 剪片精華助手

網站工具：上傳影片 → Whisper 轉文字 + 音量分析 → 揀出精華片段 → ffmpeg 剪片合併。

有設 `ANTHROPIC_API_KEY` 就用 Claude 理解內容揀精華（質素較好，會收 API 費用）；冇設就自動 fallback 做純音量分析（完全免費，但準確度較低）。

## 本機開發

1. 安裝 ffmpeg（需要 Homebrew）:
   ```bash
   brew install ffmpeg
   ```
2. （選填）想用 Claude 分析就設定 API key（喺 console.anthropic.com/settings/keys 攞）:
   ```bash
   export ANTHROPIC_API_KEY=sk-ant-...
   ```
3. 開啟網站:
   ```bash
   cd "/Users/yinkingpoon/Desktop/Video"
   source .venv/bin/activate
   uvicorn backend.app.main:app --reload --port 8000
   ```
   然後開瀏覽器去 http://localhost:8000。本機開發冇設 `SITE_PASSWORD` 嘅話唔會要求登入。

## 部署去 Railway（公開網站）

1. 去 [railway.app](https://railway.app) 用你嘅 GitHub 帳戶登入，建立一個新 project，揀「Deploy from GitHub repo」，指向呢個 repo（要先推去 GitHub）。Railway 會自動偵測到 `Dockerfile` 嚟build。
2. 喺 project 嘅 **Variables** 分頁加入呢幾個環境變數:
   - `SITE_PASSWORD` — 你想用嘅登入密碼（設咗先會要求登入，好重要，唔設就任何人都入得）
   - `SITE_USERNAME` — 登入用戶名（選填，預設 `admin`）
   - `MAX_DURATION_SECONDS` — 建議設 `1200`（20 分鐘），因為免費/入門方案記憶體有限（例如 1GB），太長嘅片會爆記憶體
   - `ANTHROPIC_API_KEY` — （選填）你嘅 Anthropic key，設咗就會用 Claude 揀精華，唔設就用免費嘅音量分析
3. 部署完成後，Railway 會俾一個 `*.up.railway.app` 網址，開嗰個網址會彈出瀏覽器原生登入視窗，輸入返上面設嘅用戶名密碼就用得。

## 注意事項

- 第一次轉文字會自動下載 Whisper 語音模型（Railway 版本已經 bake 咗入 Docker image）。
- Railway 版本冇持久儲存，剪好嘅精華片請即刻download落嚟，重新部署或者容器重啟後舊片會被清走。
- 上傳大檔案（成條片）需要時間，視乎你同 Railway 之間嘅網速。
- 本機（例如你自己部 16GB Mac）記憶體充足，可以處理長片；`MAX_DURATION_SECONDS` 預設喺本機冇實際限制。
