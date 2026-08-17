# 剪片精華助手

本機用嘅網站工具：上傳影片 → Whisper 轉文字 + 音量分析 → Claude 揀精華片段 → ffmpeg 剪片合併。

## 本機開發

1. 安裝 ffmpeg（需要 Homebrew）:
   ```bash
   brew install ffmpeg
   ```
2. 設定環境變數（Anthropic API key 喺 console.anthropic.com/settings/keys 攞）:
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
   - `ANTHROPIC_API_KEY` — 你嘅 Anthropic key
   - `SITE_PASSWORD` — 你想用嘅登入密碼（設咗先會要求登入，好重要，唔設就任何人都入得）
   - `SITE_USERNAME` — 登入用戶名（選填，預設 `admin`）
3. 部署完成後，Railway 會俾一個 `*.up.railway.app` 網址，開嗰個網址會彈出瀏覽器原生登入視窗，輸入返上面設嘅用戶名密碼就用得。

## 注意事項

- 第一次轉文字會自動下載 Whisper 語音模型（細模型，約幾百 MB，Railway 版本已經 bake 咗入 Docker image）。
- 每次 AI 揀精華都會用你自己嘅 Anthropic API key 收費，片愈長／揀嘅片段愈多，用嘅 token 就愈多。
- Railway 版本冇持久儲存，剪好嘅精華片請即刻download落嚟，重新部署或者容器重啟後舊片會被清走。
- 上傳大檔案（成條片）需要時間，視乎你同 Railway 之間嘅網速。
