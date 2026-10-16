from fastapi import FastAPI
from pydantic import BaseModel
import yt_dlp, pathlib, uuid, os, shutil

app = FastAPI(title="YouTube Downloader Backend")
TMP = pathlib.Path("/tmp/yt")
TMP.mkdir(parents=True, exist_ok=True)

class DownloadRequest(BaseModel):
    url: str
    format: str = "mp4"
    filename: str | None = None

@app.get("/health")
def health():
    return {"ok": True, "status": "online"}

@app.get("/")
def root():
    return {"ok": True}

@app.post("/api/download")
def download(req: DownloadRequest):
    try:
        url = req.url.strip()
        fmt = req.format.lower()
        # اعتبارسنجی ساده
        if "youtu" not in url:
            return {"ok": False, "error": "invalid_url"}

        uid = uuid.uuid4().hex[:8]
        out = str(TMP / f"{uid}.%(ext)s")

        if fmt == "mp3":
            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": out,
                "noplaylist": True,
                "quiet": True,
                "postprocessors": [{"key": "FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":"128"}],
            }
        else: # mp4
            ydl_opts = {
                "format": "best[height<=720][ext=mp4]/best[ext=mp4]/best",
                "outtmpl": out,
                "noplaylist": True,
                "quiet": True,
                "merge_output_format": "mp4",
            }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title","YouTube")
            # پیدا کردن فایل
            files = list(TMP.glob(f"{uid}.*"))
            if not files:
                return {"ok": False, "error": "file_not_created"}
            fpath = max(files, key=lambda p: p.stat().st_size)
            # انتقال به مسیر ثابت برای دانلود
            final_name = f"{uid}_{fpath.suffix}"
            final_path = TMP / final_name
            shutil.move(str(fpath), str(final_path))
            file_url = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME','youtube-downloader-backend-3-kzax.onrender.com')}/file/{final_name}"
            # اگر روی Render نیستی، از BASE_URL استفاده کن
            base = os.getenv("BASE_URL")
            if base:
                file_url = base.rstrip("/") + f"/file/{final_name}"
            return {"ok": True, "file_url": file_url, "url": file_url, "title": title, "file_size": final_path.stat().st_size}

    except Exception as e:
        # مهم: هرگز 500 نده، همیشه JSON برگردان
        return {"ok": False, "error": "download_failed", "detail": str(e)[:400]}

@app.get("/file/{name}")
def get_file(name: str):
    from fastapi.responses import FileResponse
    p = TMP / name
    if not p.exists():
        return {"ok": False, "error": "not_found"}
    return FileResponse(str(p), filename=name)
