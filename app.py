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
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from faster_whisper import WhisperModel
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
# 例: "chrome" / "safari"。ログインが必要なリールを取得するときに使う
COOKIES_FROM_BROWSER = os.getenv("YTDLP_COOKIES_FROM_BROWSER", "").strip()

INSTAGRAM_URL = re.compile(r"^https?://(www\.)?instagram\.com/(reel|reels|p|tv)/[\w-]+", re.I)

app = FastAPI(title="Reel Translator")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


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


ENV_PATH = BASE_DIR / ".env"


def key_configured() -> bool:
    # サーバー起動後に .env を作成・編集しても反映されるよう毎回読み直す
    load_dotenv(ENV_PATH, override=True)
    key = os.getenv("ANTHROPIC_API_KEY", "")
    return key.startswith("sk-ant-") and "xxxx" not in key


def get_claude() -> anthropic.Anthropic:
    if not key_configured():
        raise HTTPException(400, "Claude の APIキーが設定されていません。画面上の「APIキー設定」から登録してください。")
    return anthropic.Anthropic()


def save_key_to_env(key: str) -> None:
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    lines = [l for l in lines if not l.startswith("ANTHROPIC_API_KEY=")]
    lines.insert(0, f"ANTHROPIC_API_KEY={key}")
    ENV_PATH.write_text("\n".join(lines) + "\n")
    ENV_PATH.chmod(0o600)  # 自分以外は読めないようにする
    os.environ["ANTHROPIC_API_KEY"] = key


class KeyRequest(BaseModel):
    key: str


@app.get("/api/key")
def key_status():
    return {"configured": key_configured()}


@app.post("/api/key")
def set_key(req: KeyRequest):
    key = req.key.strip()
    if not key.startswith("sk-ant-"):
        raise HTTPException(400, "APIキーは「sk-ant-」で始まる文字列です。コピーし直してください。")
    # 保存前に本当に使えるキーか確認する
    try:
        anthropic.Anthropic(api_key=key).models.list(limit=1)
    except anthropic.AuthenticationError:
        raise HTTPException(400, "この APIキーは無効です。Anthropic Console でキーを確認してください。")
    except anthropic.APIConnectionError:
        raise HTTPException(502, "Anthropic に接続できませんでした。ネット接続を確認してください。")
    save_key_to_env(key)
    return {"configured": True}


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


def translate(client: anthropic.Anthropic, transcript: str, caption: str) -> dict:
    user_content = (
        f"<transcript>\n{transcript or '(発話なし)'}\n</transcript>\n\n"
        f"<caption>\n{caption or '(なし)'}\n</caption>\n\n"
        "上記の Instagram リールの文字起こしと投稿キャプションを日本語に翻訳し、要約も作成してください。"
    )
    response = client.beta.messages.create(
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
    # 時間のかかるダウンロード・文字起こしの前に APIキーを確認する
    client = get_claude()

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
        result = translate(client, transcript, caption)
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
