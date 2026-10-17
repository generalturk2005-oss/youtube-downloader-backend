from fastapi import FastAPI
from fastapi.responses import FileResponse
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
    url = req.url.strip()
    fmt = req.format.lower().strip()
    
    if "youtu" not in url:
        return {"ok": False, "error": "invalid_url", "detail": "لینک یوتیوب معتبر نیست"}

    uid = uuid.uuid4().hex[:8]
    out = str(TMP / f"{uid}.%(ext)s")

    # لیست client هایی که امتحان میکنیم — هر کدام بلاک شد بعدی را امتحان میکند
    clients_to_try = [["android"], ["android","web"], ["ios"], ["web"]]

    last_error = ""
    for client in clients_to_try:
        try:
            ydl_opts = {
                "format": "best[height<=720][ext=mp4]/best[ext=mp4]/best" if fmt == "mp4" else "bestaudio/best",
                "outtmpl": out,
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "geo_bypass": True,
                "nocheckcertificate": True,
                "socket_timeout": 30,
                "extractor_args": {"youtube": {"player_client": client}},
                "http_headers": {"User-Agent": "com.google.android.youtube/17.31.35 (Linux; U; Android 11) gzip"},
            }
            # اگر cookies.txt کنار main.py آپلود کرده باشی خودکار استفاده میشود
            if pathlib.Path("cookies.txt").exists():
                ydl_opts["cookiefile"] = "cookies.txt"

            if fmt == "mp3":
                ydl_opts["postprocessors"] = [{"key": "FFmpegExtractAudio","preferredcodec": "mp3","preferredquality": "128"}]
            else:
                ydl_opts["merge_output_format"] = "mp4"

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get("title", "YouTube")
                files = list(TMP.glob(f"{uid}.*"))
                if not files:
                    return {"ok": False, "error": "file_not_created"}
                fpath = max(files, key=lambda p: p.stat().st_size)
                final_name = f"{uid}{fpath.suffix}"
                final_path = TMP / final_name
                shutil.move(str(fpath), str(final_path))
                host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "youtube-downloader-backend-3-kzax.onrender.com")
                base = os.getenv("BASE_URL", f"https://{host}")
                file_url = base.rstrip("/") + f"/file/{final_name}"
                return {"ok": True, "file_url": file_url, "url": file_url, "title": title, "file_size": final_path.stat().st_size}

        except Exception as e:
            last_error = str(e)[:500]
            # اگر خطای "Sign in to confirm" بود، client بعدی را امتحان کن
            if "Sign in to confirm" in last_error or "not a bot" in last_error:
                continue
            else:
                # خطای دیگر — همین را برگردان
                return {"ok": False, "error": "download_failed", "detail": last_error}
    
    # همه client ها بلاک شدند
    return {"ok": False, "error": "download_failed", "detail": last_error[:400]}

@app.get("/file/{name}")
def get_file(name: str):
    p = TMP / name
    if not p.exists():
        return {"ok": False, "error": "not_found"}
    return FileResponse(str(p), filename=name)
