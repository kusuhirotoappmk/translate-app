"""Instagram リール → 英語の文字起こし → 日本語訳 を返す Web アプリ。

APIキー不要・無料。文字起こしも翻訳もこの Mac の中で動くモデルで行う。
"""

import os
import re
import tempfile
import threading
from functools import lru_cache
from pathlib import Path

import yt_dlp
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
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

INSTAGRAM_URL = re.compile(r"^https?://(www\.)?instagram\.com/(reel|reels|p|tv)/[\w-]+", re.I)

app = FastAPI(title="Reel Translator")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# モデルは同時に1リクエストずつしか扱えないためロックする
model_lock = threading.Lock()


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    # 想定外のエラーも画面で読める JSON で返す
    return JSONResponse(status_code=500, content={"detail": f"予期しないエラーが発生しました: {exc}"})


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
        info = ydl.extract_info(url, download=True)
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
    with model_lock:
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


@app.post("/api/translate")
def translate_reel(req: TranslateRequest):
    url = req.url.strip()
    if not INSTAGRAM_URL.match(url):
        raise HTTPException(400, "Instagram のリール URL を入力してください。")

    with tempfile.TemporaryDirectory() as workdir:
        try:
            path, info = download_audio(url, workdir)
        except yt_dlp.utils.DownloadError as e:
            raise HTTPException(
                502,
                "動画を取得できませんでした。非公開投稿か、ログインが必要な可能性があります"
                "（.env の YTDLP_COOKIES_FROM_BROWSER を設定してください）。"
                f"\n詳細: {e}",
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
