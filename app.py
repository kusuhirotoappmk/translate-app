"""Instagram リール → 英語の文字起こし → 日本語訳 を返す Web アプリ。"""

import json
import os
import re
import tempfile
from functools import lru_cache
from pathlib import Path

import anthropic
import yt_dlp
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from faster_whisper import WhisperModel
from pydantic import BaseModel

load_dotenv()

BASE_DIR = Path(__file__).parent
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
# 例: "chrome" / "safari"。ログインが必要なリールを取得するときに使う
COOKIES_FROM_BROWSER = os.getenv("YTDLP_COOKIES_FROM_BROWSER", "").strip()

INSTAGRAM_URL = re.compile(r"^https?://(www\.)?instagram\.com/(reel|reels|p|tv)/[\w-]+", re.I)

app = FastAPI(title="Reel Translator")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


class TranslateRequest(BaseModel):
    url: str


@lru_cache(maxsize=1)
def get_whisper() -> WhisperModel:
    # 初回のみモデルをダウンロード・ロードする
    return WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")


@lru_cache(maxsize=1)
def get_claude() -> anthropic.Anthropic:
    return anthropic.Anthropic()


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


TRANSLATION_SCHEMA = {
    "type": "object",
    "properties": {
        "summary_ja": {"type": "string", "description": "内容の要約（日本語、3〜5文）"},
        "translation_ja": {"type": "string", "description": "発話内容の自然な日本語訳（全文）"},
        "caption_ja": {"type": "string", "description": "投稿キャプションの日本語訳（無ければ空文字）"},
    },
    "required": ["summary_ja", "translation_ja", "caption_ja"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "あなたは英語の SNS 動画を日本語で紹介するプロの翻訳者です。"
    "文字起こしは音声認識によるもので誤認識を含む場合があります。文脈から自然に補って訳してください。"
    "スラングや固有名詞は意味が伝わるように訳し、必要なら括弧で原語を添えてください。"
)


def translate(transcript: str, caption: str) -> dict:
    user_content = (
        f"<transcript>\n{transcript or '(発話なし)'}\n</transcript>\n\n"
        f"<caption>\n{caption or '(なし)'}\n</caption>\n\n"
        "上記の Instagram リールの文字起こしと投稿キャプションを日本語に翻訳し、要約も作成してください。"
    )
    response = get_claude().beta.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": TRANSLATION_SCHEMA},
        },
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise HTTPException(422, "この動画の内容は翻訳できませんでした。")
    text = next((b.text for b in response.content if b.type == "text"), "")
    return json.loads(text)


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
    try:
        result = translate(transcript, caption)
    except anthropic.AuthenticationError:
        raise HTTPException(500, "Claude API キーが無効です。.env の ANTHROPIC_API_KEY を確認してください。")
    except anthropic.RateLimitError:
        raise HTTPException(429, "API の利用上限に達しました。しばらくしてから再度お試しください。")
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"翻訳 API でエラーが発生しました: {e.message}")
    except anthropic.APIConnectionError:
        raise HTTPException(502, "翻訳 API に接続できませんでした。")

    return {
        "title": info.get("title") or "",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "transcript_en": transcript,
        "caption_en": caption,
        **result,
    }
