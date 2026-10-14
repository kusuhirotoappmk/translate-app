# translate-app（リール翻訳）

Instagram のリール URL を貼ると、英語の内容を日本語テキスト（要約・全文訳・キャプション訳）で表示する Web アプリです。
**APIキー不要・完全無料。** 文字起こしも翻訳もこの Mac の中で動きます。

## しくみ

1. **yt-dlp** でリールの音声を取得
2. **faster-whisper** で英語を文字起こし
3. **MLX + Qwen3-4B**（Apple Silicon 向けのローカルAIモデル）で日本語に翻訳・要約

## 動作環境

- Apple Silicon（M1〜M4 など）の Mac、メモリ 8GB 以上推奨
- Python 3.10 以上
- 初回のみモデルのダウンロード（合計 約2.8GB）が発生します

## セットアップ

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 起動

```bash
.venv/bin/uvicorn app:app --port 8000
```

ブラウザで http://localhost:8000 を開き、リールの URL を貼って「翻訳する」を押します。

## 補足

- 取得できない場合（ログインが必要な投稿など）は `.env.example` を `.env` にコピーし、`YTDLP_COOKIES_FROM_BROWSER` に Instagram にログイン済みのブラウザ名（`chrome` など）を設定してください。
- `.env` は `.gitignore` 済みで、GitHub には上がりません。

## インターネットに公開する（URL を知っている人が使えるようにする）

[cloudflared](https://github.com/cloudflare/cloudflared) を入れた状態で次を実行します。

```bash
./start-public.sh
```

- `https://xxxx.trycloudflare.com` の公開URLが表示され、誰でもそのURLからアプリを使えます（Mac が起動中で、このスクリプトを動かしている間だけ）。
- 公開URLは起動するたびに変わります。最新のURLは `docs/live.json` に書き込まれて GitHub に push され、紹介ページの「アプリを開く」ボタンに自動で反映されます。
- 公開時の負荷対策として、同時処理は1件まで、1人あたり1時間に10回まで、3分以内の動画のみに制限しています（`.env` の `RATE_LIMIT_PER_HOUR` / `MAX_DURATION_SEC` で変更可）。

## 紹介ページ

`docs/` フォルダが GitHub Pages で公開されるポートフォリオ用の紹介ページです。
