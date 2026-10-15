"""PDF Splitter: upload a scanned PDF, rotate pages, split into fixed-size files, zip.

Nothing is kept: every job directory is deleted JOB_TTL seconds after upload.
"""
import asyncio
import hmac
import io
import os
import re
import secrets
import shutil
import threading
import time
import zipfile
import zlib
from contextlib import asynccontextmanager
from pathlib import Path

import pypdfium2 as pdfium
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pypdf import PdfReader, PdfWriter

PIN = os.environ.get("PDF_SPLITTER_PIN", "")
DATA_DIR = Path(os.environ.get("PDF_SPLITTER_DATA", "data")).resolve()
JOB_TTL = int(os.environ.get("PDF_SPLITTER_TTL_SECONDS", 15 * 60))
MAX_UPLOAD = int(os.environ.get("PDF_SPLITTER_MAX_UPLOAD_MB", 500)) * 1024 * 1024
THUMB_WIDTH = 220
MAX_FAILURES, LOCKOUT_SECONDS = 5, 60

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
pdfium_lock = threading.Lock()  # pdfium is not thread-safe
failures: dict[str, list[float]] = {}


def clear_data_dir() -> None:
    """Empty DATA_DIR but keep the directory itself (it may be a mount point)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for p in DATA_DIR.iterdir():
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)


def purge_expired(now: float | None = None) -> None:
    now = now or time.time()
    with jobs_lock:
        expired = [j for j, m in jobs.items() if now - m["created"] >= JOB_TTL]
        for j in expired:
            del jobs[j]
    for j in expired:
        shutil.rmtree(DATA_DIR / j, ignore_errors=True)
    # Anything on disk we don't know about (e.g. left by a crash) goes too.
    for p in DATA_DIR.iterdir():
        if p.name not in jobs and p.is_dir():
            shutil.rmtree(p, ignore_errors=True)


async def janitor():
    while True:
        await asyncio.sleep(30)
        await asyncio.to_thread(purge_expired)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not PIN:
        raise RuntimeError("Set PDF_SPLITTER_PIN to the pass pin teachers must enter.")
    clear_data_dir()
    task = asyncio.create_task(janitor())
    yield
    task.cancel()
    clear_data_dir()


app = FastAPI(lifespan=lifespan)


def get_job(job_id: str) -> dict:
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or time.time() - job["created"] >= JOB_TTL:
        raise HTTPException(404, "This upload has expired or does not exist.")
    return job


def check_pin(request: Request, pin: str) -> None:
    ip = request.client.host if request.client else "?"
    now = time.time()
    recent = [t for t in failures.get(ip, []) if now - t < LOCKOUT_SECONDS]
    if len(recent) >= MAX_FAILURES:
        raise HTTPException(429, "Too many wrong pins. Wait a minute and try again.")
    if not hmac.compare_digest(pin.encode(), PIN.encode()):
        failures[ip] = recent + [now]
        raise HTTPException(403, "Incorrect pin.")
    failures.pop(ip, None)


def safe_base(filename: str) -> str:
    stem = Path(filename or "document").stem
    return re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "document"


@app.post("/api/upload")
async def upload(request: Request, username: str = Form(...), pin: str = Form(...),
                 file: UploadFile = File(...)):
    check_pin(request, pin)
    username = username.strip()
    if not username or len(username) > 50:
        raise HTTPException(400, "Enter a user name (up to 50 characters).")
    job_id = secrets.token_urlsafe(16)
    job_dir = DATA_DIR / job_id
    job_dir.mkdir()
    path = job_dir / "original.pdf"
    try:
        size = 0
        with open(path, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, f"File is larger than {MAX_UPLOAD // 2**20} MB.")
                out.write(chunk)
        try:
            reader = PdfReader(path)
            if reader.is_encrypted:
                raise HTTPException(400, "Password-protected PDFs are not supported.")
            pages = len(reader.pages)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(400, "That does not look like a valid PDF.")
        if pages == 0:
            raise HTTPException(400, "The PDF has no pages.")
    except BaseException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    with jobs_lock:
        jobs[job_id] = {"created": time.time(), "username": username, "pages": pages,
                        "base": safe_base(file.filename), "zips": {}}
    return {"id": job_id, "pages": pages, "ttl": JOB_TTL, "base": jobs[job_id]["base"]}


@app.get("/api/jobs/{job_id}")
def job_info(job_id: str):
    job = get_job(job_id)
    return {"pages": job["pages"], "remaining": max(0, int(JOB_TTL - (time.time() - job["created"])))}


@app.get("/api/jobs/{job_id}/thumb/{page}")
def thumb(job_id: str, page: int):
    job = get_job(job_id)
    if not 0 <= page < job["pages"]:
        raise HTTPException(404)
    cache = DATA_DIR / job_id / f"thumb_{page}.jpg"
    if not cache.exists():
        with pdfium_lock:
            pdf = pdfium.PdfDocument(str(DATA_DIR / job_id / "original.pdf"))
            try:
                p = pdf[page]
                img = p.render(scale=THUMB_WIDTH / p.get_width()).to_pil().convert("RGB")
            finally:
                pdf.close()
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70)
        cache.write_bytes(buf.getvalue())
    return FileResponse(cache, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=900"})


class SplitRequest(BaseModel):
    pages_per_file: int = Field(ge=1)
    max_zip_mb: float | None = Field(default=None, gt=0)
    rotations: dict[int, int] = {}  # 0-based page -> clockwise degrees


ZIP_OVERHEAD = 22  # end-of-central-directory record


def entry_cost(name: str, data: bytes) -> int:
    """Upper-ish bound of the bytes one deflated zip entry adds to an archive."""
    c = zlib.compressobj(6, zlib.DEFLATED, -15)
    packed = len(c.compress(data)) + len(c.flush())
    return packed + 30 + 46 + 2 * len(name.encode()) + 32


@app.post("/api/jobs/{job_id}/split")
def split(job_id: str, req: SplitRequest):
    job = get_job(job_id)
    job_dir = DATA_DIR / job_id
    limit = int(req.max_zip_mb * 1024 * 1024) if req.max_zip_mb else None
    total, base = job["pages"], job["base"]
    for p, deg in req.rotations.items():
        if not 0 <= p < total or deg % 90:
            raise HTTPException(400, "Invalid rotation.")

    for old in job["zips"].values():
        (job_dir / old["file"]).unlink(missing_ok=True)
    job["zips"] = {}

    reader = PdfReader(job_dir / "original.pdf")
    width = len(str(total))
    # Build each fixed-size PDF, pack greedily into zips under the size limit.
    groups: list[list[tuple[str, bytes]]] = [[]]
    used = ZIP_OVERHEAD
    for start in range(0, total, req.pages_per_file):
        end = min(start + req.pages_per_file, total)
        writer = PdfWriter()
        for i in range(start, end):
            page = reader.pages[i]
            deg = req.rotations.get(i, 0) % 360
            if deg:
                page.rotate(deg)
            writer.add_page(page)
        buf = io.BytesIO()
        writer.write(buf)
        data = buf.getvalue()
        name = f"{base}_pages_{start + 1:0{width}d}-{end:0{width}d}.pdf"
        cost = entry_cost(name, data)
        if limit and ZIP_OVERHEAD + cost > limit:
            raise HTTPException(400, (
                f"Pages {start + 1}-{end} make a {cost / 2**20:.1f} MB file, which is bigger than the "
                f"{req.max_zip_mb:g} MB zip limit. Use fewer pages per file or a larger limit."))
        if limit and groups[-1] and used + cost > limit:
            groups.append([])
            used = ZIP_OVERHEAD
        groups[-1].append((name, data))
        used += cost

    out = []
    for n, files in enumerate(groups, 1):
        zname = f"{base}.zip" if len(groups) == 1 else f"{base}_{n:0{len(str(len(groups)))}d}of{len(groups)}.zip"
        zpath = job_dir / zname
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in files:
                z.writestr(name, data)
        size = zpath.stat().st_size
        if limit and size > limit:  # estimate was optimistic; never hand out an oversize zip
            raise HTTPException(500, "Could not fit files under the zip limit; try a slightly smaller limit.")
        job["zips"][zname] = {"file": zname}
        out.append({"name": zname, "size": size, "files": len(files),
                    "url": f"/api/jobs/{job_id}/zips/{zname}"})
    return {"zips": out}


@app.get("/api/jobs/{job_id}/zips/{name}")
def download(job_id: str, name: str):
    job = get_job(job_id)
    if name not in job["zips"]:
        raise HTTPException(404)
    return FileResponse(DATA_DIR / job_id / name, media_type="application/zip", filename=name)


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
