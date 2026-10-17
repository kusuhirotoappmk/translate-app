#!/bin/bash
# アプリを起動し、Cloudflare Tunnel で公開URLを付ける。
# 公開URLは docs/live.json に書き込んで GitHub に push し、紹介ページの「アプリを開く」ボタンに反映する。
# Mac のスリープやネット切断で公開URLが使えなくなったら、自動で新しいURLを取り直す。
# 止めるときは Ctrl+C。
set -uo pipefail
cd "$(dirname "$0")"

CLOUDFLARED="${CLOUDFLARED:-$HOME/.local/bin/cloudflared}"
PORT=8000
CHECK_INTERVAL=30  # 公開URLの生存確認の間隔（秒）
MAX_FAILURES=3     # この回数続けてつながらなければ取り直す
LOG="$(mktemp -t cloudflared)"
APP_PID=""
TUNNEL_PID=""
URL=""

log() { echo "[$(date '+%H:%M:%S')] $*"; }

cleanup() {
  echo ""
  log "停止しています…"
  kill $APP_PID $TUNNEL_PID $CAFFEINATE_PID 2>/dev/null || true
  rm -f "$LOG"
  exit 0
}
trap cleanup INT TERM

start_app() {
  log "アプリを起動しています…"
  .venv/bin/uvicorn app:app --host 127.0.0.1 --port "$PORT" &
  APP_PID=$!
}

publish_url() {
  printf '{\n  "url": "%s",\n  "updated": "%s"\n}\n' "$URL" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > docs/live.json
  if git add docs/live.json && git commit -q -m "公開URLを更新" -- docs/live.json && git push -q; then
    log "紹介ページの公開URLを更新しました（反映まで1〜2分）"
  else
    log "※ 紹介ページへの反映（git push）に失敗しました。次回の取り直し時に再度試します"
  fi
}

start_tunnel() {
  # 古いトンネルを止めてから、新しい公開URLを取れるまで繰り返す
  [ -n "$TUNNEL_PID" ] && kill "$TUNNEL_PID" 2>/dev/null
  while true; do
    log "公開URLを発行しています…"
    : > "$LOG"
    "$CLOUDFLARED" tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" >"$LOG" 2>&1 &
    TUNNEL_PID=$!
    URL=""
    for _ in $(seq 1 60); do
      URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1 || true)"
      [ -n "$URL" ] && break
      sleep 1
    done
    if [ -n "$URL" ]; then
      break
    fi
    log "公開URLを取得できませんでした（ネット未接続の可能性）。30秒後に再試行します"
    kill "$TUNNEL_PID" 2>/dev/null
    sleep 30
  done
  # URL 発行直後は DNS が浸透しておらずつながらないことがあるので少し待つ
  sleep 10
  publish_url
  echo ""
  echo "=============================================="
  echo " 公開中: $URL"
  echo " 止めるときは Ctrl+C"
  echo "=============================================="
}

# 起動中に Mac がアイドルでスリープしないようにする（ふたを閉じた場合はスリープします）
caffeinate -i -w $$ &
CAFFEINATE_PID=$!

start_app
start_tunnel

failures=0
while true; do
  sleep "$CHECK_INTERVAL"

  if ! kill -0 "$APP_PID" 2>/dev/null; then
    log "アプリが停止していたので再起動します"
    start_app
    sleep 5
  fi

  if curl -s -m 10 -o /dev/null -f "$URL/api/health"; then
    failures=0
  else
    failures=$((failures + 1))
    log "公開URLにつながりません（${failures}/${MAX_FAILURES}）"
    if [ "$failures" -ge "$MAX_FAILURES" ] || grep -q "Tunnel not found" "$LOG"; then
      log "公開URLが失効したので取り直します"
      start_tunnel
      failures=0
    fi
  fi
done
