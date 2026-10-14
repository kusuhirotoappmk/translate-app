#!/bin/bash
# アプリを起動し、Cloudflare Tunnel で公開URLを付ける。
# 公開URLは docs/live.json に書き込んで GitHub に push し、紹介ページの「アプリを開く」ボタンに反映する。
# 止めるときは Ctrl+C。
set -euo pipefail
cd "$(dirname "$0")"

CLOUDFLARED="${CLOUDFLARED:-$HOME/.local/bin/cloudflared}"
PORT=8000
LOG="$(mktemp -t cloudflared)"

cleanup() {
  echo ""
  echo "停止しています…"
  kill "${APP_PID:-}" "${TUNNEL_PID:-}" 2>/dev/null || true
  rm -f "$LOG"
}
trap cleanup EXIT INT TERM

echo "アプリを起動しています…"
.venv/bin/uvicorn app:app --host 127.0.0.1 --port "$PORT" &
APP_PID=$!

echo "公開URLを発行しています…"
"$CLOUDFLARED" tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" >"$LOG" 2>&1 &
TUNNEL_PID=$!

URL=""
for _ in $(seq 1 60); do
  URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1 || true)"
  [ -n "$URL" ] && break
  sleep 1
done
if [ -z "$URL" ]; then
  echo "公開URLを取得できませんでした。ログ:"
  cat "$LOG"
  exit 1
fi

# 紹介ページ用に最新の公開URLを記録して push する
printf '{\n  "url": "%s",\n  "updated": "%s"\n}\n' "$URL" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > docs/live.json
if git add docs/live.json && git commit -q -m "公開URLを更新" -- docs/live.json && git push -q; then
  echo "紹介ページの公開URLを更新しました（反映まで1〜2分）"
else
  echo "※ 紹介ページへの反映（git push）に失敗しました。アプリ自体は下のURLで使えます"
fi

echo ""
echo "=============================================="
echo " 公開中: $URL"
echo " 止めるときは Ctrl+C"
echo "=============================================="
wait "$APP_PID"
