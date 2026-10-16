import os
import asyncio
import socket
import ipaddress
import tempfile
import shutil
import glob
import uuid
import logging
from urllib.parse import urlparse
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks, Header, Request
from pydantic import BaseModel, HttpUrl
from starlette.responses import FileResponse, JSONResponse
import yt_dlp

logger = logging.getLogger("ytdl_backend")
logging.basicConfig(level=logging.INFO)

# Configuration from environment
API_SECRET = os.getenv("API_SECRET")
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2"))
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "120"))  # seconds
MAX_FILE_SIZE_BYTES = int(os.getenv("MAX_FILE_SIZE_BYTES", str(100 * 1024 * 1024)))  # default 100 MB
TMP_ROOT = os.getenv("TMP_ROOT", tempfile.gettempdir())

app = FastAPI(title="YouTube Downloader Backend")


class DownloadRequest(BaseModel):
    url: HttpUrl
    format: str = "mp4"  # supported: mp4, mp3
    filename: Optional[str] = None


@app.on_event("startup")
async def startup_event():
    # Initialize concurrency limiter
    app.state.semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
    if not API_SECRET:
        logger.warning("API_SECRET not set. /api/download will reject requests until API_SECRET is configured.")


@app.get("/health")
async def health():
    return {"ok": True, "status": "online"}


def _cleanup_dir(path: str):
    try:
        shutil.rmtree(path)
        logger.debug("Cleaned up tmpdir %s", path)
    except Exception as e:
        logger.warning("Failed to cleanup %s: %s", path, e)


def _select_downloaded_file(tmpdir: str, requested_format: str) -> Optional[str]:
    files = glob.glob(os.path.join(tmpdir, "**"), recursive=True)
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        return None

    if requested_format == "mp3":
        for f in files:
            if f.lower().endswith(".mp3"):
                return f
        for ext in (".m4a", ".webm", ".aac", ".ogg"):
            for f in files:
                if f.lower().endswith(ext):
                    return f
    else:
        for ext in (".mp4", ".mkv", ".webm", ".mov"):
            for f in files:
                if f.lower().endswith(ext):
                    return f

    files.sort(key=lambda p: os.path.getsize(p), reverse=True)
    return files[0]


def _is_private_or_reserved_host(hostname: str) -> bool:
    """Resolve hostname and ensure none of the resolved addresses are private/loopback/reserved."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        # Could not resolve — treat as invalid
        logger.debug("DNS resolution failed for %s", hostname)
        return True

    for family, _, _, _, sockaddr in infos:
        # sockaddr can be tuple (addr, port) for IPv4, or (addr, port, flow, scope) for IPv6
        addr = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            # Not an IP address — skip
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            logger.debug("Blocked host %s resolved to restricted IP %s", hostname, addr)
            return True
    return False


def _sanitize_filename(name: str) -> str:
    # Keep only a safe subset of filename characters and limit length
    keepchars = " ._-()"
    cleaned = "".join(c for c in name if c.isalnum() or c in keepchars)
    if not cleaned:
        cleaned = "download"
    return cleaned[:200]


def _blocking_download(url: str, ydl_opts: dict):
    # Blockingly run yt_dlp download — intended to be used inside run_in_executor
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])


@app.post("/api/download")
async def download_endpoint(req: DownloadRequest, background_tasks: BackgroundTasks, authorization: Optional[str] = Header(None)):
    # Authentication
    if not API_SECRET:
        raise HTTPException(status_code=500, detail="Server API_SECRET not configured")

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = authorization.split(" ", 1)[1]
    if token != API_SECRET:
        raise HTTPException(status_code=403, detail="Invalid API token")

    # Validate format
    fmt = req.format.lower()
    if fmt not in ("mp4", "mp3"):
        raise HTTPException(status_code=400, detail="Unsupported format. Use 'mp4' or 'mp3'.")

    # SSRF protection: ensure host is not private/reserved
    parsed = urlparse(str(req.url))
    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(status_code=400, detail="Invalid URL host")
    if _is_private_or_reserved_host(hostname):
        raise HTTPException(status_code=400, detail="URL resolves to a disallowed IP address")

    # Concurrency limit
    sem: asyncio.Semaphore = app.state.semaphore
    acquired = await sem.acquire()

    tmpdir = os.path.join(TMP_ROOT, "ytdl", str(uuid.uuid4()))
    os.makedirs(tmpdir, exist_ok=True)
    outtmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")

    ydl_opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
    }

    if fmt == "mp3":
        ydl_opts.update(
            {
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
            }
        )
    else:
        ydl_opts.update({"format": "bestvideo+bestaudio/best"})

    loop = asyncio.get_event_loop()
    try:
        # Run the blocking download in a thread and apply an overall timeout
        await asyncio.wait_for(loop.run_in_executor(None, _blocking_download, str(req.url), ydl_opts), timeout=DOWNLOAD_TIMEOUT)
    except asyncio.TimeoutError:
        _cleanup_dir(tmpdir)
        raise HTTPException(status_code=504, detail="Download timed out")
    except Exception as e:
        _cleanup_dir(tmpdir)
        logger.exception("Download failed")
        raise HTTPException(status_code=500, detail=f"Download failed: {e}")
    finally:
        sem.release()

    filepath = _select_downloaded_file(tmpdir, "mp3" if fmt == "mp3" else "mp4")
    if not filepath or not os.path.exists(filepath):
        _cleanup_dir(tmpdir)
        raise HTTPException(status_code=500, detail="Downloaded file not found")

    # File size limit check
    try:
        size = os.path.getsize(filepath)
    except OSError:
        _cleanup_dir(tmpdir)
        raise HTTPException(status_code=500, detail="Could not inspect downloaded file size")

    if size > MAX_FILE_SIZE_BYTES:
        _cleanup_dir(tmpdir)
        raise HTTPException(status_code=413, detail=f"File too large ({size} bytes). Limit is {MAX_FILE_SIZE_BYTES} bytes")

    # Determine response filename
    if req.filename:
        safe_name = _sanitize_filename(req.filename)
    else:
        safe_name = os.path.basename(filepath)

    headers = {"Content-Disposition": f'attachment; filename="{safe_name}"'}

    # Schedule cleanup
    background_tasks.add_task(_cleanup_dir, tmpdir)

    # Serve file
    return FileResponse(path=filepath, media_type="application/octet-stream", filename=safe_name, headers=headers)


@app.get("/")
async def root():
    return JSONResponse({"message": "YouTube downloader backend. See /health and POST /api/download"})
