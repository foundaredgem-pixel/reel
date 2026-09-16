import base64, os, re, secrets, shutil, subprocess, threading, time, uuid, json
from pathlib import Path
from queue import Queue
from typing import Any

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
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

WEBHOOK_VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN", "").strip()

APPROVED_SENDER_IDS = {
    x.strip()
    for x in os.getenv("APPROVED_SENDER_IDS", "").split(",")
    if x.strip()
}

MAX_DOWNLOAD_MB = int(os.getenv("MAX_DOWNLOAD_MB", "200"))
MAX_DURATION_SECONDS = int(os.getenv("MAX_DURATION_SECONDS", "900"))

SHARE_TO_FEED = os.getenv("SHARE_TO_FEED", "true").lower() not in {
    "0",
    "false",
    "no",
}

IG_POLL_SECONDS = int(os.getenv("IG_POLL_SECONDS", "10"))
IG_MAX_POLLS = int(os.getenv("IG_MAX_POLLS", "36"))

INSTAGRAM_COOKIES_B64 = os.getenv("INSTAGRAM_COOKIES_B64", "").strip()

jobs = {}
jobs_lock = threading.Lock()

manual_active_job = None

dm_queue = Queue()


app = FastAPI(title="Insta Reel Poster")


class PostRequest(BaseModel):
    reel_url: str = Field(min_length=10, max_length=2000)
    caption: str = Field(default="", max_length=2200)
    app_key: str = Field(default="", max_length=500)


def set_job(j, **u):
    with jobs_lock:
        if j in jobs:
            jobs[j].update(u)


def public_url(request=None):
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL

    if not request:
        return ""

    return (
        f"{request.headers.get('x-forwarded-proto', request.url.scheme)}://"
        f"{request.headers.get('x-forwarded-host', request.headers.get('host', ''))}"
    ).rstrip("/")


def valid_url(u):
    return bool(
        re.match(
            r"^https?://(www\.)?instagram\.com/(reel|reels|p)/[^/?#]+",
            u.strip(),
            re.I,
        )
    )


def reel_url(u):
    return bool(
        re.search(
            r"instagram\.com/(reel|reels)/",
            u,
            re.I,
        )
    )


def cmd(c, timeout, cwd=None):
    return subprocess.run(
        c,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def cookies(d):
    if not INSTAGRAM_COOKIES_B64:
        return None

    try:
        data = base64.b64decode(
            INSTAGRAM_COOKIES_B64,
            validate=True,
        )
    except Exception as e:
        raise RuntimeError(
            "INSTAGRAM_COOKIES_B64 is not valid base64."
        ) from e

    p = d / "cookies.txt"
    p.write_bytes(data)

    return p


def caption_from_reel(url, d):
    c = cookies(d)

    x = [
        "yt-dlp",
        "--no-cache-dir",
        "--skip-download",
        "--no-warnings",
        "--dump-single-json",
        url,
    ]

    if c:
        x[1:1] = ["--cookies", str(c)]

    try:
        r = cmd(x, 90, d)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            "Timed out while reading the Reel caption."
        ) from e

    if r.returncode:
        raise RuntimeError(
            "The Reel was detected, but its caption could not be read. "
            + (r.stderr or r.stdout)[-1200:]
        )

    try:
        i = json.loads(r.stdout)
    except Exception as e:
        raise RuntimeError(
            "The Reel caption response could not be read."
        ) from e

    return str(
        i.get("description")
        or i.get("title")
        or ""
    ).replace("\x00", "").strip()[:2200]


def download(url, out, d, j):
    set_job(
        j,
        status="downloading",
        message="Downloading Reel…",
    )

    c = cookies(d)

    x = [
        "yt-dlp",
        "--no-cache-dir",
        "--no-part",
        "--retries",
        "2",
        "--fragment-retries",
        "2",
        "--socket-timeout",
        "30",
        "--max-filesize",
        f"{MAX_DOWNLOAD_MB}M",
        "--format",
        "best[ext=mp4][height<=1080]/best[height<=1080]/best[ext=mp4]/best",
        "--merge-output-format",
        "mp4",
        "--output",
        str(out),
        url,
    ]

    if c:
        x[1:1] = ["--cookies", str(c)]

    try:
        r = cmd(x, 180, d)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            "Instagram download timed out after 3 minutes."
        ) from e

    if r.returncode:
        raise RuntimeError(
            "Could not download this Instagram Reel. "
            + (r.stderr or r.stdout)[-1800:]
        )

    vs = [
        p
        for p in d.glob("*")
        if p.is_file()
        and p.suffix.lower()
        in {".mp4", ".mov", ".webm", ".mkv"}
    ]

    if not vs:
        raise RuntimeError(
            "The downloader finished but no video file was produced."
        )

    s = max(
        vs,
        key=lambda p: p.stat().st_size,
    )

    if s != out:
        shutil.move(str(s), str(out))

    if not out.exists() or not out.stat().st_size:
        raise RuntimeError(
            "Downloaded video file is empty."
        )

    if out.stat().st_size > MAX_DOWNLOAD_MB * 1024 * 1024:
        raise RuntimeError(
            f"Downloaded file exceeds the {MAX_DOWNLOAD_MB} MB limit."
        )


def normalize(src, out, j):
    set_job(
        j,
        status="processing",
        message="Preparing video…",
    )

    vf = (
        "scale=w=720:h=1280:"
        "force_original_aspect_ratio=decrease,"
        "pad=720:1280:(ow-iw)/2:(oh-ih)/2:color=black,"
        "fps=30,format=yuv420p"
    )

    x = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-threads",
        "1",
        "-filter_threads",
        "1",
        "-filter_complex_threads",
        "1",
        "-i",
        str(src),
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "28",
        "-maxrate",
        "4M",
        "-bufsize",
        "8M",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        "-t",
        str(MAX_DURATION_SECONDS),
        "-y",
        str(out),
    ]

    try:
        r = cmd(x, 300, src.parent)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            "Video processing timed out after 5 minutes."
        ) from e

    if r.returncode:
        raise RuntimeError(
            "FFmpeg could not prepare the video: "
            + (r.stderr or r.stdout)[-1800:]
        )

    if not out.exists() or not out.stat().st_size:
        raise RuntimeError(
            "FFmpeg finished but the processed video is empty."
        )


def graph_post(j, video, cover, cap):
    set_job(
        j,
        status="uploading",
        message="Sending Reel to Instagram…",
    )

    base = f"https://graph.instagram.com/{IG_API_VERSION}"

    if not IG_ACCESS_TOKEN or not IG_USER_ID:
        raise RuntimeError(
            "Instagram publishing credentials are not configured on Render."
        )

    p = {
        "media_type": "REELS",
        "video_url": video,
        "caption": cap,
        "share_to_feed": "true" if SHARE_TO_FEED else "false",
        "cover_url": cover,
        "access_token": IG_ACCESS_TOKEN,
    }

    r = requests.post(
        f"{base}/{IG_USER_ID}/media",
        data=p,
        timeout=60,
    )

    try:
        d = r.json()
    except ValueError:
        raise RuntimeError(
            f"Instagram container request returned HTTP "
            f"{r.status_code} with a non-JSON response."
        )

    if r.status_code >= 400 or "error" in d:
        raise RuntimeError(
            f"Instagram container error: {d}"
        )

    cid = d.get("id")

    if not cid:
        raise RuntimeError(
            f"Instagram did not return a creation ID: {d}"
        )

    for _ in range(IG_MAX_POLLS):
        time.sleep(IG_POLL_SECONDS)

        r = requests.get(
            f"{base}/{cid}",
            params={
                "fields": "status_code,status",
                "access_token": IG_ACCESS_TOKEN,
            },
            timeout=60,
        )

        try:
            d = r.json()
        except ValueError:
            raise RuntimeError(
                f"Instagram status check returned HTTP "
                f"{r.status_code} with a non-JSON response."
            )

        if r.status_code >= 400 or "error" in d:
            raise RuntimeError(
                f"Instagram status error: {d}"
            )

        code = str(
            d.get("status_code", "")
        ).upper()

        if code == "FINISHED":
            break

        if code in {"ERROR", "EXPIRED"}:
            raise RuntimeError(
                f"Instagram processing failed: {d}"
            )

    else:
        raise RuntimeError(
            "Instagram took too long to finish processing the Reel."
        )

    r = requests.post(
        f"{base}/{IG_USER_ID}/media_publish",
        data={
            "creation_id": cid,
            "access_token": IG_ACCESS_TOKEN,
        },
        timeout=60,
    )

    try:
        d = r.json()
    except ValueError:
        raise RuntimeError(
            f"Instagram publish returned HTTP "
            f"{r.status_code} with a non-JSON response."
        )

    if r.status_code >= 400 or "error" in d:
        raise RuntimeError(
            f"Instagram publish error: {d}"
        )

    if not d.get("id"):
        raise RuntimeError(
            f"Instagram publish returned no media ID: {d}"
        )

    return str(d["id"])


def dm_reply(recipient, text):
    if not recipient or not IG_ACCESS_TOKEN or not IG_USER_ID:
        return

    base = f"https://graph.instagram.com/{IG_API_VERSION}"

    r = requests.post(
        f"{base}/{IG_USER_ID}/messages",
        json={
            "recipient": {
                "id": recipient
            },
            "message": {
                "text": text[:1000]
            },
        },
        params={
            "access_token": IG_ACCESS_TOKEN
        },
        timeout=60,
    )

    if r.status_code >= 400:
        print(
            "Instagram DM reply failed:",
            r.status_code,
            r.text[:500],
            flush=True,
        )


def cleanup(p, delay):
    time.sleep(delay)
    shutil.rmtree(
        p,
        ignore_errors=True,
    )


def process(j, url, cap, base, reply=None):
    global manual_active_job

    d = MEDIA / j
    src = d / "source.mp4"
    out = d / "reel.mp4"

    try:
        if not base:
            raise RuntimeError(
                "PUBLIC_BASE_URL is not configured."
            )

        d.mkdir(
            parents=True,
            exist_ok=True,
        )

        final = (
            cap
            if cap is not None
            else caption_from_reel(url, d)
        )

        if cap is None:
            set_job(
                j,
                message="Reading Reel caption…",
            )

        download(
            url,
            src,
            d,
            j,
        )

        normalize(
            src,
            out,
            j,
        )

        src.unlink(
            missing_ok=True
        )

        pid = graph_post(
            j,
            f"{base}/media/{j}/reel.mp4",
            f"{base}/cover.jpg",
            final,
        )

        set_job(
            j,
            status="done",
            message="✅ Reel posted successfully!",
            published_id=pid,
            caption=final,
        )

        if reply:
            dm_reply(
                reply,
                "✅ Reel posted successfully!",
            )

        threading.Thread(
            target=cleanup,
            args=(d, 1800),
            daemon=True,
        ).start()

    except Exception as e:
        msg = f"❌ Reel failed\nReason: {e}"

        set_job(
            j,
            status="error",
            message=msg,
        )

        if reply:
            dm_reply(
                reply,
                msg,
            )

        threading.Thread(
            target=cleanup,
            args=(d, 300),
            daemon=True,
        ).start()

    finally:
        if INSTAGRAM_COOKIES_B64:
            try:
                (d / "cookies.txt").unlink(
                    missing_ok=True
                )
            except Exception:
                pass

        with jobs_lock:
            if manual_active_job == j:
                manual_active_job = None


def worker():
    while True:
        j, url, sender, base = dm_queue.get()

        try:
            process(
                j,
                url,
                None,
                base,
                sender,
            )
        finally:
            dm_queue.task_done()


threading.Thread(
    target=worker,
    daemon=True,
    name="instagram-dm-worker",
).start()


def find_url(o):
    if isinstance(o, dict):
        for k, v in o.items():

            if (
                isinstance(v, str)
                and k.lower()
                in {"url", "link", "media_url"}
                and "instagram.com/" in v.lower()
            ):
                return v

            z = find_url(v)

            if z:
                return z

    elif isinstance(o, list):
        for v in o:
            z = find_url(v)

            if z:
                return z

    return None


def webhook_handle(p):
    base = public_url()

    for e in p.get("entry", []):

        for ev in e.get("messaging", []):

            m = ev.get("message", {})

            if not isinstance(m, dict):
                continue

            if m.get("is_echo"):
                continue

            sender = str(
                ev.get("sender", {}).get("id", "")
            ).strip()

            # ---------------------------------------------------------
            # SENDER-ID DISCOVERY MODE
            #
            # When APPROVED_SENDER_IDS is empty, we do NOT process
            # incoming messages. We only print the sender ID so it
            # can be copied into Render.
            # ---------------------------------------------------------

            if not APPROVED_SENDER_IDS:

                if sender:
                    print(
                        f"DISCOVERED_SENDER_ID={sender}",
                        flush=True,
                    )
                else:
                    print(
                        "WEBHOOK_EVENT_WITHOUT_SENDER="
                        + json.dumps(
                            ev,
                            ensure_ascii=False,
                        )[:4000],
                        flush=True,
                    )

                continue

            # ---------------------------------------------------------
            # APPROVED SENDER CHECK
            # ---------------------------------------------------------

            if (
                not sender
                or sender not in APPROVED_SENDER_IDS
            ):
                continue

            # ---------------------------------------------------------
            # FIND SHARED INSTAGRAM MEDIA
            # ---------------------------------------------------------

            u = find_url(m)

            if not u:
                continue

            # Only Reels are processed.
            # Normal Instagram posts are ignored.
            if not reel_url(u):
                continue

            # ---------------------------------------------------------
            # CREATE QUEUED JOB
            # ---------------------------------------------------------

            j = uuid.uuid4().hex[:16]

            with jobs_lock:
                jobs[j] = {
                    "status": "queued",
                    "message": "Queued…",
                    "created_at": time.time(),
                    "source": "instagram_dm",
                    "sender_id": sender,
                    "reel_url": u,
                }

            dm_queue.put(
                (
                    j,
                    u,
                    sender,
                    base,
                )
            )


@app.get("/webhooks/instagram")
def verify(request: Request):

    if not WEBHOOK_VERIFY_TOKEN:
        raise HTTPException(
            503,
            "WEBHOOK_VERIFY_TOKEN is not configured.",
        )

    q = request.query_params

    if (
        q.get("hub.mode") == "subscribe"
        and secrets.compare_digest(
            q.get("hub.verify_token", ""),
            WEBHOOK_VERIFY_TOKEN,
        )
    ):
        return int(
            q.get(
                "hub.challenge",
                "0",
            )
        )

    raise HTTPException(
        403,
        "Webhook verification failed.",
    )


@app.post("/webhooks/instagram")
async def webhook(request: Request):

    p = await request.json()

    if isinstance(p, dict):
        webhook_handle(p)

    return {
        "ok": True
    }


@app.get("/")
def index():
    return FileResponse(
        STATIC / "index.html"
    )


@app.get("/cover.jpg")
def cover():
    return FileResponse(
        STATIC / "cover.jpg",
        media_type="image/jpeg",
    )


@app.get("/health")
def health():

    return {
        "ok": True,
        "instagram_configured": bool(
            IG_ACCESS_TOKEN and IG_USER_ID
        ),
        "app_key_configured": bool(APP_KEY),
        "dm_webhook_configured": bool(
            WEBHOOK_VERIFY_TOKEN
        ),
        "approved_sender_count": len(
            APPROVED_SENDER_IDS
        ),
    }


@app.get("/api/config")
def config():

    return {
        "app_key_required": bool(APP_KEY),
        "dm_enabled": bool(
            WEBHOOK_VERIFY_TOKEN
            and APPROVED_SENDER_IDS
        ),
    }


@app.get("/media/{job_id}/reel.mp4")
def media(job_id: str):

    p = MEDIA / job_id / "reel.mp4"

    if not p.exists():
        raise HTTPException(
            404,
            "Media not found or expired.",
        )

    return FileResponse(
        p,
        media_type="video/mp4",
        headers={
            "Cache-Control": "public,max-age=3600"
        },
    )


@app.post("/api/post")
def post(
    payload: PostRequest,
    request: Request,
):

    global manual_active_job

    if not valid_url(payload.reel_url):
        raise HTTPException(
            400,
            "Please enter a valid Instagram Reel URL.",
        )

    if (
        APP_KEY
        and not secrets.compare_digest(
            payload.app_key,
            APP_KEY,
        )
    ):
        raise HTTPException(
            401,
            "Invalid app key.",
        )

    if not IG_ACCESS_TOKEN or not IG_USER_ID:
        raise HTTPException(
            500,
            "Instagram credentials are not configured.",
        )

    with jobs_lock:

        if (
            manual_active_job
            and jobs.get(
                manual_active_job,
                {},
            ).get("status")
            not in {"done", "error"}
        ):
            raise HTTPException(
                409,
                "Another Reel is already being processed. Please wait.",
            )

        j = uuid.uuid4().hex[:16]

        manual_active_job = j

        jobs[j] = {
            "status": "starting",
            "message": "Starting…",
            "created_at": time.time(),
            "source": "web",
        }

    threading.Thread(
        target=process,
        args=(
            j,
            payload.reel_url.strip(),
            payload.caption.strip(),
            public_url(request),
            None,
        ),
        daemon=True,
    ).start()

    return {
        "job_id": j
    }


@app.get("/api/status/{job_id}")
def status(job_id: str):

    with jobs_lock:
        d = jobs.get(job_id)

    if not d:
        raise HTTPException(
            404,
            "Job not found.",
        )

    return d
