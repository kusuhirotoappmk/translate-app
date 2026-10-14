# translate-app（リール翻訳）

Instagram のリール URL を貼ると、英語の内容を日本語テキスト（要約・全文訳・キャプション訳）で表示する Web アプリです。

## しくみ

1. **yt-dlp** でリールの音声を取得
2. **faster-whisper** で英語を文字起こし（PC 上で実行・無料）
3. **Claude API**（Claude Opus 5.5）で日本語に翻訳・要約

## セットアップ

Python 3.10 以上が必要です。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # .env に ANTHROPIC_API_KEY を記入
```

## 起動

```bash
.venv/bin/uvicorn app:app --port 8000
```

ブラウザで http://localhost:8000 を開き、リールの URL を貼って「翻訳する」を押します。
初回は文字起こしモデルのダウンロードがあるため少し時間がかかります。

## 補足

- 取得できない場合（ログインが必要な投稿など）は `.env` の `YTDLP_COOKIES_FROM_BROWSER` に Instagram にログイン済みのブラウザ名（`chrome` など）を設定してください。
- `.env`（API キー）は `.gitignore` 済みで、GitHub には上がりません。
