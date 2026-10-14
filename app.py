"""Instagram リール → 英語の文字起こし → 日本語訳 を返す Web アプリ。

APIキー不要・無料。文字起こしも翻訳もこの Mac の中で動くモデルで行う。
"""

import os
import re
import tempfile
import threading
import time
from collections import defaultdict, deque
from functools import lru_cache
from pathlib import Path

import yt_dlp
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from faster_whisper import WhisperModel
from mlx_lm import generate, load
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
# Apple Silicon 向けの無料ローカル翻訳モデル（初回のみ約2.3GBダウンロード）
LLM_MODEL = os.getenv("LLM_MODEL", "mlx-community/Qwen3-4B-Instruct-2507-4bit")
# 例: "chrome" / "safari"。ログインが必要なリールを取得するときに使う
COOKIES_FROM_BROWSER = os.getenv("YTDLP_COOKIES_FROM_BROWSER", "").strip()

# 公開時の負荷対策
MAX_DURATION_SEC = int(os.getenv("MAX_DURATION_SEC", "180"))  # これより長い動画は断る
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_HOUR", "10"))  # 1人あたり1時間の回数

INSTAGRAM_URL = re.compile(r"^https?://(www\.)?instagram\.com/(reel|reels|p|tv)/[\w-]+", re.I)

app = FastAPI(title="Reel Translator")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
# 紹介ページ（GitHub Pages）から稼働状況を確認できるようにする
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"])

# 同時に処理するのは1件だけ（Mac の負荷を抑える）
busy_lock = threading.Lock()
recent_requests: dict[str, deque] = defaultdict(deque)


def check_rate_limit(request: Request) -> None:
    # Cloudflare 経由のときは本来のアクセス元 IP がこのヘッダーに入る
    ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")
    now = time.time()
    history = recent_requests[ip]
    while history and now - history[0] > 3600:
        history.popleft()
    if len(history) >= RATE_LIMIT:
        raise HTTPException(429, f"1時間に{RATE_LIMIT}回までです。しばらくしてからお試しください。")
    history.append(now)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    # 想定外のエラーも画面で読める JSON で返す
    print(f"Unexpected error: {exc!r}")
    return JSONResponse(status_code=500, content={"detail": "予期しないエラーが発生しました。時間をおいてもう一度お試しください。"})


class TranslateRequest(BaseModel):
    url: str


@lru_cache(maxsize=1)
def get_whisper() -> WhisperModel:
    # 初回のみモデルをダウンロード・ロードする
    return WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")


@lru_cache(maxsize=1)
def get_llm():
    return load(LLM_MODEL)


def download_audio(url: str, workdir: str) -> tuple[Path, dict]:
    opts = {
        # 音声のみを取得（ffmpeg 不要）。無ければ動画ごと取得
        "format": "bestaudio/best",
        "outtmpl": os.path.join(workdir, "media.%(ext)s"),
        "quiet": True,
        "noplaylist": True,
    }
    if COOKIES_FROM_BROWSER:
        opts["cookiesfrombrowser"] = (COOKIES_FROM_BROWSER,)
    with yt_dlp.YoutubeDL(opts) as ydl:
        # 先に情報だけ取得して、長すぎる動画はダウンロードしない
        info = ydl.extract_info(url, download=False)
        if (info.get("duration") or 0) > MAX_DURATION_SEC:
            raise HTTPException(400, f"{MAX_DURATION_SEC // 60}分以内の動画にしてください。")
        info = ydl.process_ie_result(info, download=True)
        path = Path(ydl.prepare_filename(info))
    return path, info


def transcribe(path: Path) -> str:
    segments, _ = get_whisper().transcribe(str(path), language="en", vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


SYSTEM_PROMPT = (
    "あなたは英語の SNS 動画を日本語で紹介するプロの翻訳者です。"
    "文字起こしは音声認識によるもので誤認識を含む場合があります。文脈から自然に補って訳してください。"
    "スラングや固有名詞は意味が伝わるように訳してください。"
)


def ask_llm(instruction: str, text: str) -> str:
    model, tokenizer = get_llm()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"{instruction}\n\n{text}"},
    ]
    prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
    return generate(model, tokenizer, prompt=prompt, max_tokens=2048).strip()


def translate(transcript: str, caption: str) -> dict:
    translation = summary = caption_ja = ""
    if transcript:
        translation = ask_llm("次の英語を自然な日本語に全文翻訳してください。訳文だけを出力してください。", transcript)
        summary = ask_llm(
            "次の動画の内容を日本語で3〜5文に要約してください。要約だけを出力してください。",
            f"{transcript}\n\n(投稿キャプション: {caption})" if caption else transcript,
        )
    elif caption:
        summary = "（動画内で話されている言葉は検出されませんでした）"
    if caption:
        caption_ja = ask_llm("次の Instagram 投稿キャプションを自然な日本語に翻訳してください。訳文だけを出力してください。", caption)
    return {"translation_ja": translation, "summary_ja": summary, "caption_ja": caption_ja}


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "busy": busy_lock.locked()}


@app.post("/api/translate")
def translate_reel(req: TranslateRequest, request: Request):
    url = req.url.strip()
    if not INSTAGRAM_URL.match(url):
        raise HTTPException(400, "Instagram のリール URL を入力してください。")
    if not busy_lock.acquire(blocking=False):
        raise HTTPException(429, "ほかの人の翻訳を処理中です。1分ほど待ってからもう一度お試しください。")
    try:
        check_rate_limit(request)
        return process_reel(url)
    finally:
        busy_lock.release()


def process_reel(url: str) -> dict:
    with tempfile.TemporaryDirectory() as workdir:
        try:
            path, info = download_audio(url, workdir)
        except yt_dlp.utils.DownloadError as e:
            # 詳細はサーバー側のログにだけ残し、利用者には簡潔に伝える
            print(f"DownloadError: {e}")
            raise HTTPException(
                502,
                "動画を取得できませんでした。URL が正しいか、公開されている投稿かを確認してください。",
            )
        transcript = transcribe(path)

    caption = info.get("description") or ""
    return {
        "title": info.get("title") or "",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "transcript_en": transcript,
        "caption_en": caption,
        **translate(transcript, caption),
    }
