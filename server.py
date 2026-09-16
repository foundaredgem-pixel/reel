import base64
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

BASE = Path(__file__).resolve().parent
MEDIA = BASE / "media"
STATIC = BASE / "static"
MEDIA.mkdir(exist_ok=True)
STATIC.mkdir(exist_ok=True)

IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN", "").strip()
IG_USER_ID = os.getenv("IG_USER_ID", "").strip()
IG_API_VERSION = os.getenv("IG_API_VERSION", "v26.0").strip()
APP_KEY = os.getenv("APP_KEY", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
MAX_DOWNLOAD_MB = int(os.getenv("MAX_DOWNLOAD_MB", "200"))
MAX_DURATION_SECONDS = int(os.getenv("MAX_DURATION_SECONDS", "900"))
SHARE_TO_FEED = os.getenv("SHARE_TO_FEED", "true").lower() == "true"
POLL_SECONDS = int(os.getenv("IG_POLL_SECONDS", "10"))
MAX_POLLS = int(os.getenv("IG_MAX_POLLS", "36"))

JOBS = {}
LOCK = threading.Lock()


class PostRequest(BaseModel):
    reel_url: str = Field(min_length=10, max_length=2000)
    caption: str = Field(default="", max_length=2200)
    app_key: str = Field(default="", max_length=500)


app = FastAPI(title="Simple Instagram Reel Poster", version="1.0.0")


def fail(message):
    raise RuntimeError(message)


def valid_instagram_url(url: str) -> bool:
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        return p.scheme in ("http", "https") and (
            host == "instagram.com" or host.endswith(".instagram.com")
        )
    except Exception:
        return False


def run_ffmpeg(input_path: Path, output_path: Path):
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(input_path),
        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,setsar=1",
        "-r", "30",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "48000",
        "-movflags", "+faststart",
        str(output_path),
    ]
    subprocess.run(cmd, check=True, timeout=900)


def download_reel(url: str, job_dir: Path) -> Path:
    cookie_file = job_dir / "cookies.txt"
    cookie_b64 = os.getenv("INSTAGRAM_COOKIES_B64", "").strip()
    if cookie_b64:
        try:
            cookie_file.write_bytes(base64.b64decode(cookie_b64))
        except Exception as exc:
            fail(f"INSTAGRAM_COOKIES_B64 is invalid: {exc}")

    outtmpl = str(job_dir / "source.%(ext)s")
    opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 30,
        "max_filesize": MAX_DOWNLOAD_MB * 1024 * 1024,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
    }
    if cookie_file.exists():
        opts["cookiefile"] = str(cookie_file)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:
        raise RuntimeError(
            "Instagram could not provide the Reel to the downloader. "
            "The Reel may require login, be unavailable, or Instagram may be "
            f"rate-limiting extraction. Details: {exc}"
        ) from exc

    duration = info.get("duration")
    if duration and duration > MAX_DURATION_SECONDS:
        fail(f"Video is {int(duration)} seconds; maximum is {MAX_DURATION_SECONDS} seconds.")

    candidates = [p for p in job_dir.glob("source.*") if p.is_file()]
    candidates = [p for p in candidates if p.name != "cookies.txt"]
    if not candidates:
        fail("The downloader completed but no video file was produced.")
    return max(candidates, key=lambda p: p.stat().st_size)


def public_base(request: Request) -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", ""))
    if not host:
        fail("Set PUBLIC_BASE_URL in the deployment environment.")
    return f"{proto}://{host}"


def instagram_call(path: str, data: dict):
    url = f"https://graph.instagram.com/{IG_API_VERSION}/{path.lstrip('/')}"
    try:
        r = requests.post(url, data=data, timeout=90)
    except requests.RequestException as exc:
        fail(f"Instagram API network error: {exc}")
    if not r.ok:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        fail(f"Instagram API error ({r.status_code}): {detail}")
    return r.json()


def instagram_get(path: str, params: dict):
    url = f"https://graph.instagram.com/{IG_API_VERSION}/{path.lstrip('/')}"
    try:
        r = requests.get(url, params=params, timeout=60)
    except requests.RequestException as exc:
        fail(f"Instagram API network error: {exc}")
    if not r.ok:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        fail(f"Instagram API error ({r.status_code}): {detail}")
    return r.json()


def publish_reel(video_url: str, cover_url: str, caption: str):
    if not IG_ACCESS_TOKEN or not IG_USER_ID:
        fail("IG_ACCESS_TOKEN and IG_USER_ID must be set on the server.")

    data = {
        "media_type": "REELS",
        "video_url": video_url,
        "cover_url": cover_url,
        "caption": caption,
        "share_to_feed": str(SHARE_TO_FEED).lower(),
        "access_token": IG_ACCESS_TOKEN,
    }
    created = instagram_call(f"{IG_USER_ID}/media", data)
    container_id = created.get("id")
    if not container_id:
        fail(f"Instagram did not return a container ID: {created}")

    last = {}
    for _ in range(MAX_POLLS):
        time.sleep(POLL_SECONDS)
        last = instagram_get(
            container_id,
            {"fields": "status_code,status", "access_token": IG_ACCESS_TOKEN},
        )
        status = last.get("status_code")
        if status == "FINISHED":
            break
        if status in {"ERROR", "EXPIRED"}:
            fail(f"Instagram processing failed: {last}")
    else:
        fail(f"Instagram processing timed out: {last}")

    published = instagram_call(
        f"{IG_USER_ID}/media_publish",
        {"creation_id": container_id, "access_token": IG_ACCESS_TOKEN},
    )
    return published.get("id", "")


def set_job(job_id, **values):
    with LOCK:
        JOBS[job_id].update(values)


def worker(job_id: str, request: Request, payload: PostRequest):
    job_dir = MEDIA / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        set_job(job_id, status="downloading", message="Downloading Reel…")
        source = download_reel(payload.reel_url, job_dir)

        set_job(job_id, status="processing", message="Preparing video…")
        processed = job_dir / "reel.mp4"
        run_ffmpeg(source, processed)

        base = public_base(request)
        video_url = f"{base}/media/{job_id}/reel.mp4"
        cover_url = f"{base}/cover.jpg"

        set_job(job_id, status="uploading", message="Sending Reel to Instagram…")
        media_id = publish_reel(video_url, cover_url, payload.caption.strip())

        set_job(
            job_id,
            status="done",
            message="Reel posted successfully.",
            instagram_media_id=media_id,
        )
    except Exception as exc:
        set_job(job_id, status="error", message=str(exc))
    finally:
        try:
            if (job_dir / "cookies.txt").exists():
                (job_dir / "cookies.txt").unlink()
        except Exception:
            pass


@app.get("/")
def home():
    return FileResponse(STATIC / "index.html")


@app.get("/cover.jpg")
def cover():
    return FileResponse(STATIC / "cover.jpg", media_type="image/jpeg")


@app.get("/health")
def health():
    return {
        "ok": True,
        "instagram_configured": bool(IG_ACCESS_TOKEN and IG_USER_ID),
        "app_key_configured": bool(APP_KEY),
    }


@app.get("/media/{job_id}/reel.mp4")
def media(job_id: str):
    path = MEDIA / job_id / "reel.mp4"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Media not found")
    return FileResponse(path, media_type="video/mp4")


@app.post("/api/post")
def start_post(payload: PostRequest, request: Request):
    if APP_KEY and not secrets.compare_digest(payload.app_key, APP_KEY):
        raise HTTPException(status_code=401, detail="Wrong app key.")

    if not valid_instagram_url(payload.reel_url):
        raise HTTPException(status_code=400, detail="Please enter an Instagram URL.")

    with LOCK:
        active = [
            j for j in JOBS.values()
            if j.get("status") in {"queued", "downloading", "processing", "uploading"}
        ]
        if active:
            raise HTTPException(
                status_code=409,
                detail="A Reel is already being processed. Please wait for it to finish.",
            )

    job_id = secrets.token_urlsafe(12)
    with LOCK:
        JOBS[job_id] = {"status": "queued", "message": "Queued…"}

    threading.Thread(
        target=worker,
        args=(job_id, request, payload),
        daemon=True,
    ).start()

    return {"job_id": job_id}


@app.get("/api/status/{job_id}")
def status(job_id: str):
    with LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/api/config")
def config():
    return {"app_key_required": bool(APP_KEY)}
