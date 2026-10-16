from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel, HttpUrl
from starlette.responses import FileResponse, JSONResponse
import yt_dlp
import tempfile
import os
import glob
import shutil
import uuid

app = FastAPI(title="YouTube Downloader Backend")


class DownloadRequest(BaseModel):
    url: HttpUrl
    format: str = "mp4"  # supported: mp4, mp3
    filename: str | None = None


@app.get("/health")
async def health():
    return {"ok": True, "status": "online"}


def _cleanup_dir(path: str):
    try:
        shutil.rmtree(path)
    except Exception:
        pass


def _select_downloaded_file(tmpdir: str, requested_format: str) -> str | None:
    files = glob.glob(os.path.join(tmpdir, "**"), recursive=True)
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        return None

    if requested_format == "mp3":
        # prefer .mp3
        for f in files:
            if f.lower().endswith(".mp3"):
                return f
        # fallback to any audio-like
        for ext in (".m4a", ".webm", ".aac", ".ogg"):
            for f in files:
                if f.lower().endswith(ext):
                    return f
    else:
        # prefer common video extensions
        for ext in (".mp4", ".mkv", ".webm", ".mov"):
            for f in files:
                if f.lower().endswith(ext):
                    return f
    # fallback to the largest file
    files.sort(key=lambda p: os.path.getsize(p), reverse=True)
    return files[0]


@app.post("/api/download")
async def download_endpoint(req: DownloadRequest, background_tasks: BackgroundTasks):
    # basic validation of format
    fmt = req.format.lower()
    if fmt not in ("mp4", "mp3"):
        raise HTTPException(status_code=400, detail="Unsupported format. Use 'mp4' or 'mp3'.")

    tmpdir = os.path.join(tempfile.gettempdir(), "ytdl", str(uuid.uuid4()))
    os.makedirs(tmpdir, exist_ok=True)

    outtmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")

    ydl_opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
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
        # mp4 (video)
        ydl_opts.update({"format": "bestvideo+bestaudio/best"})

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([str(req.url)])
    except Exception as e:
        _cleanup_dir(tmpdir)
        raise HTTPException(status_code=500, detail=f"Download failed: {e}")

    filepath = _select_downloaded_file(tmpdir, "mp3" if fmt == "mp3" else "mp4")
    if not filepath or not os.path.exists(filepath):
        _cleanup_dir(tmpdir)
        raise HTTPException(status_code=500, detail="Downloaded file not found")

    # determine response filename
    resp_filename = req.filename if req.filename else os.path.basename(filepath)

    headers = {
        "Content-Disposition": f'attachment; filename="{resp_filename}"'
    }

    background_tasks.add_task(_cleanup_dir, tmpdir)

    # Use FileResponse so the file is streamed from disk
    return FileResponse(path=filepath, media_type="application/octet-stream", filename=resp_filename, headers=headers)


@app.get("/")
async def root():
    return JSONResponse({"message": "YouTube downloader backend. See /health and POST /api/download"})
