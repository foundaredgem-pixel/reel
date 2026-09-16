import base64
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
MEDIA = ROOT / "media"
MEDIA.mkdir(exist_ok=True)

IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN", "").strip()
IG_USER_ID = os.getenv("IG_USER_ID", "").strip()
IG_API_VERSION = os.getenv("IG_API_VERSION", "v26.0").strip()
APP_KEY = os.getenv("APP_KEY", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")

MAX_DOWNLOAD_MB = int(os.getenv("MAX_DOWNLOAD_MB", "200"))
MAX_DURATION_SECONDS = int(os.getenv("MAX_DURATION_SECONDS", "900"))
SHARE_TO_FEED = os.getenv("SHARE_TO_FEED", "true").lower() not in {"0", "false", "no"}
IG_POLL_SECONDS = int(os.getenv("IG_POLL_SECONDS", "10"))
IG_MAX_POLLS = int(os.getenv("IG_MAX_POLLS", "36"))
INSTAGRAM_COOKIES_B64 = os.getenv("INSTAGRAM_COOKIES_B64", "").strip()

jobs: dict[str, dict[str, Any]] = {}
jobs_lock = threading.Lock()
active_job: str | None = None

app = FastAPI(title="Insta Reel Poster")


class PostRequest(BaseModel):
    reel_url: str = Field(min_length=10, max_length=2000)
    caption: str = Field(default="", max_length=2200)
    app_key: str = Field(default="", max_length=500)


def set_job(job_id: str, **updates: Any) -> None:
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(updates)


def get_public_base_url(request: Request | None = None) -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    if request is None:
        return ""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", ""))
    return f"{proto}://{host}".rstrip("/")


def valid_instagram_url(url: str) -> bool:
    return bool(re.match(r"^https?://(www\.)?instagram\.com/(reel|reels|p)/[^/?#]+", url.strip(), re.I))


def run_command(cmd: list[str], timeout: int, cwd: Path | None = None) -> subprocess.CompletedProcess:
    # Keep subprocess memory/CPU pressure low on Render's 512 MB free instance.
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def write_cookies(job_dir: Path) -> Path | None:
    if not INSTAGRAM_COOKIES_B64:
        return None
    try:
        data = base64.b64decode(INSTAGRAM_COOKIES_B64, validate=True)
    except Exception as exc:
        raise RuntimeError("INSTAGRAM_COOKIES_B64 is not valid base64.") from exc
    cookie_file = job_dir / "cookies.txt"
    cookie_file.write_bytes(data)
    return cookie_file


def download_reel(url: str, out_file: Path, job_dir: Path) -> None:
    set_job(job_dir.name, status="downloading", message="Downloading Reel…")

    cookie_file = write_cookies(job_dir)

    # Prefer a single-file MP4 at <=1080p. This avoids downloading oversized 2K/4K
    # sources and keeps peak RAM/disk pressure low on Render Free.
    format_selector = (
        "best[ext=mp4][height<=1080]/"
        "best[height<=1080]/"
        "best[ext=mp4]/best"
    )

    cmd = [
        "yt-dlp",
        "--no-cache-dir",
        "--no-part",
        "--retries", "2",
        "--fragment-retries", "2",
        "--socket-timeout", "30",
        "--max-filesize", f"{MAX_DOWNLOAD_MB}M",
        "--format", format_selector,
        "--merge-output-format", "mp4",
        "--output", str(out_file),
        url,
    ]
    if cookie_file:
        cmd[1:1] = ["--cookies", str(cookie_file)]

    try:
        result = run_command(cmd, timeout=180, cwd=job_dir)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Instagram download timed out after 3 minutes.") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        # Do not expose cookies or environment variables in the error.
        detail = detail[-1800:]
        raise RuntimeError(
            "Could not download this Instagram Reel. Instagram may require login, "
            "rate-limit the server, or have changed its media delivery. "
            f"Downloader details: {detail}"
        )

    candidates = list(job_dir.glob("*"))
    videos = [p for p in candidates if p.is_file() and p.suffix.lower() in {".mp4", ".mov", ".webm", ".mkv"}]
    if not videos:
        raise RuntimeError("The downloader finished but no video file was produced.")

    source = max(videos, key=lambda p: p.stat().st_size)
    if source != out_file:
        shutil.move(str(source), str(out_file))

    if not out_file.exists() or out_file.stat().st_size == 0:
        raise RuntimeError("Downloaded video file is empty.")

    if out_file.stat().st_size > MAX_DOWNLOAD_MB * 1024 * 1024:
        raise RuntimeError(f"Downloaded file exceeds the {MAX_DOWNLOAD_MB} MB limit.")


def normalize_video(source: Path, output: Path, job_id: str) -> None:
    set_job(job_id, status="processing", message="Preparing video…")

    # 720x1280 is a deliberate memory-saving choice. It is a valid vertical Reel
    # size and substantially lowers FFmpeg's peak RAM use versus 1080x1920.
    vf = (
        "scale=w=720:h=1280:force_original_aspect_ratio=decrease,"
        "pad=720:1280:(ow-iw)/2:(oh-ih)/2:color=black,"
        "fps=30,format=yuv420p"
    )

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-threads", "1",
        "-filter_threads", "1",
        "-filter_complex_threads", "1",
        "-i", str(source),
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "28",
        "-maxrate", "4M",
        "-bufsize", "8M",
        "-c:a", "aac",
        "-b:a", "96k",
        "-ar", "44100",
        "-ac", "2",
        "-movflags", "+faststart",
        "-t", str(MAX_DURATION_SECONDS),
        "-y",
        str(output),
    ]

    try:
        result = run_command(cmd, timeout=300, cwd=source.parent)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Video processing timed out after 5 minutes.") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"FFmpeg could not prepare the video: {detail[-1800:]}")

    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("FFmpeg finished but the processed video is empty.")


def graph_post(job_id: str, video_url: str, cover_url: str, caption: str) -> str:
    set_job(job_id, status="uploading", message="Sending Reel to Instagram…")

    if not IG_ACCESS_TOKEN or not IG_USER_ID:
        raise RuntimeError("Instagram publishing credentials are not configured on Render.")

    base = f"https://graph.instagram.com/{IG_API_VERSION}"
    media_url = f"{base}/{IG_USER_ID}/media"

    payload = {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
        "share_to_feed": "true" if SHARE_TO_FEED else "false",
        "cover_url": cover_url,
        "access_token": IG_ACCESS_TOKEN,
    }

    response = requests.post(media_url, data=payload, timeout=60)
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(
            f"Instagram container request returned HTTP {response.status_code} "
            "with a non-JSON response."
        )

    if response.status_code >= 400 or "error" in data:
        raise RuntimeError(f"Instagram container error: {data}")

    creation_id = data.get("id")
    if not creation_id:
        raise RuntimeError(f"Instagram did not return a creation ID: {data}")

    status_url = f"{base}/{creation_id}"
    for _ in range(IG_MAX_POLLS):
        time.sleep(IG_POLL_SECONDS)
        status_response = requests.get(
            status_url,
            params={"fields": "status_code,status", "access_token": IG_ACCESS_TOKEN},
            timeout=60,
        )
        try:
            status_data = status_response.json()
        except ValueError:
            raise RuntimeError(
                f"Instagram status check returned HTTP {status_response.status_code} "
                "with a non-JSON response."
            )

        if status_response.status_code >= 400 or "error" in status_data:
            raise RuntimeError(f"Instagram status error: {status_data}")

        code = str(status_data.get("status_code", "")).upper()
        if code == "FINISHED":
            break
        if code in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"Instagram processing failed: {status_data}")
    else:
        raise RuntimeError("Instagram took too long to finish processing the Reel.")

    publish_url = f"{base}/{IG_USER_ID}/media_publish"
    publish_response = requests.post(
        publish_url,
        data={"creation_id": creation_id, "access_token": IG_ACCESS_TOKEN},
        timeout=60,
    )
    try:
        publish_data = publish_response.json()
    except ValueError:
        raise RuntimeError(
            f"Instagram publish returned HTTP {publish_response.status_code} "
            "with a non-JSON response."
        )

    if publish_response.status_code >= 400 or "error" in publish_data:
        raise RuntimeError(f"Instagram publish error: {publish_data}")

    published_id = publish_data.get("id")
    if not published_id:
        raise RuntimeError(f"Instagram publish returned no media ID: {publish_data}")

    return str(published_id)


def process_job(job_id: str, reel_url: str, caption: str, public_base_url: str) -> None:
    global active_job

    job_dir = MEDIA / job_id
    source = job_dir / "source.mp4"
    output = job_dir / "reel.mp4"

    try:
        if not public_base_url:
            raise RuntimeError(
                "PUBLIC_BASE_URL is not configured. Set it to your Render URL, "
                "for example https://reel-72t1.onrender.com"
            )

        job_dir.mkdir(parents=True, exist_ok=True)

        download_reel(reel_url, source, job_dir)
        normalize_video(source, output, job_id)

        # Remove the original source immediately to reduce disk pressure.
        try:
            source.unlink(missing_ok=True)
        except Exception:
            pass

        video_url = f"{public_base_url}/media/{job_id}/reel.mp4"
        cover_url = f"{public_base_url}/cover.jpg"

        published_id = graph_post(job_id, video_url, cover_url, caption)

        set_job(
            job_id,
            status="done",
            message="✅ Reel posted successfully!",
            published_id=published_id,
        )

        # Keep the processed file available briefly for Instagram to fetch.
        # It is deleted automatically after 30 minutes.
        threading.Thread(
            target=delayed_cleanup,
            args=(job_dir, 1800),
            daemon=True,
        ).start()

    except Exception as exc:
        set_job(job_id, status="error", message=f"❌ {exc}")
        # Error jobs can be cleaned sooner because Instagram will not need them.
        threading.Thread(
            target=delayed_cleanup,
            args=(job_dir, 300),
            daemon=True,
        ).start()
    finally:
        if INSTAGRAM_COOKIES_B64:
            try:
                cookie_path = job_dir / "cookies.txt"
                cookie_path.unlink(missing_ok=True)
            except Exception:
                pass
        with jobs_lock:
            active_job = None


def delayed_cleanup(path: Path, delay: int) -> None:
    time.sleep(delay)
    shutil.rmtree(path, ignore_errors=True)


@app.get("/")
def index():
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


@app.get("/api/config")
def config():
    return {"app_key_required": bool(APP_KEY)}


@app.get("/media/{job_id}/reel.mp4")
def media(job_id: str):
    file_path = MEDIA / job_id / "reel.mp4"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Media not found or expired.")
    return FileResponse(
        file_path,
        media_type="video/mp4",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.post("/api/post")
def start_post(payload: PostRequest, request: Request):
    global active_job

    if not valid_instagram_url(payload.reel_url):
        raise HTTPException(status_code=400, detail="Please enter a valid Instagram Reel URL.")

    if APP_KEY and not secrets.compare_digest(payload.app_key, APP_KEY):
        raise HTTPException(status_code=401, detail="Invalid app key.")

    if not IG_ACCESS_TOKEN or not IG_USER_ID:
        raise HTTPException(status_code=500, detail="Instagram credentials are not configured.")

    with jobs_lock:
        if active_job is not None:
            existing = jobs.get(active_job)
            if existing and existing.get("status") not in {"done", "error"}:
                raise HTTPException(
                    status_code=409,
                    detail="Another Reel is already being processed. Please wait for it to finish.",
                )
        job_id = uuid.uuid4().hex[:16]
        active_job = job_id
        jobs[job_id] = {
            "status": "starting",
            "message": "Starting…",
            "created_at": time.time(),
        }

    public_base_url = get_public_base_url(request)

    thread = threading.Thread(
        target=process_job,
        args=(job_id, payload.reel_url.strip(), payload.caption.strip(), public_base_url),
        daemon=True,
    )
    thread.start()

    return {"job_id": job_id}


@app.get("/api/status/{job_id}")
def status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job
