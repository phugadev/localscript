"""Local web app: FastAPI + a single background worker. Bind to loopback only by default."""
from __future__ import annotations

import io
import os
import queue
import threading
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from . import core, db, engine

STATIC = Path(__file__).parent / "static"
MAX_URLS_PER_REQUEST = 200

_queue: "queue.Queue[str]" = queue.Queue()
_enforce_host_check = True  # DNS-rebinding guard; disabled by the CLI only when binding off-loopback


def _worker() -> None:
    while True:
        jid = _queue.get()
        try:
            job = db.get(jid)
            if job and job["status"] == "queued":  # skip jobs deleted/finished while waiting
                engine.run_job(jid)
        except Exception:  # noqa: BLE001 - keep the worker alive no matter what
            pass
        finally:
            _queue.task_done()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    # Anything left unfinished by a previous run goes back in the queue.
    for jid in db.unfinished_ids():
        db.update(jid, status="queued", detail=None)
        _queue.put(jid)
    threading.Thread(target=_worker, daemon=True, name="localscript-worker").start()
    if not os.environ.get("LOCALSCRIPT_NO_WARMUP"):
        threading.Thread(target=core.warm_up, daemon=True, name="localscript-warmup").start()
    yield


app = FastAPI(title="LocalScript", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def host_guard(request: Request, call_next):
    """Reject requests whose Host header isn't loopback, which defeats DNS-rebinding attacks
    from a web page trying to reach this local service."""
    if _enforce_host_check:
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if host not in ("localhost", "127.0.0.1", "::1"):
            return PlainTextResponse("Forbidden: this service only answers on localhost.", status_code=403)
    return await call_next(request)


# ---------------------------------------------------------------------------- pages

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


# ----------------------------------------------------------------------------- api

@app.get("/api/health")
def health():
    """Lets the desktop app recognise an already-running LocalScript server instead of starting a second."""
    return {"app": "localscript"}


@app.get("/api/models")
def models():
    return {
        "default": core.DEFAULT_MODEL,
        "models": core.model_catalog(),
    }


class JobRequest(BaseModel):
    text: str = Field(..., description="Pasted text containing one or more video / profile / playlist URLs")
    model: str = core.DEFAULT_MODEL
    language: str | None = None          # None = auto-detect
    translate: bool = False              # translate speech to English
    save_video: bool = False             # keep the HD video file (cover is always kept)
    expand: bool = True                  # expand profiles / playlists / collections
    limit: int = Field(50, ge=1, le=200) # max videos taken from each profile/playlist
    cookies_browser: str | None = None   # e.g. "chrome" — use that browser's logged-in cookies
    force: bool = False                  # re-run even if an identical finished job exists


@app.post("/api/jobs")
def create_jobs(req: JobRequest):
    if not core.is_valid_model(req.model):
        raise HTTPException(400, f"Unknown model '{req.model}'")
    if req.model == core.PARAKEET and not core.parakeet_available():
        raise HTTPException(400, "Parakeet isn't set up yet. Run scripts/install_parakeet.sh (see the README).")
    if req.cookies_browser and req.cookies_browser not in core.COOKIE_BROWSERS:
        raise HTTPException(400, f"Unsupported browser '{req.cookies_browser}'")
    sources = core.extract_urls(req.text)
    if not sources:
        raise HTTPException(400, "No video links found. Paste TikTok, Instagram Reels or YouTube/Shorts URLs.")

    urls: list[str] = []
    notes: list[str] = []
    for src in sources:
        try:
            found = core.expand_url(src, req.limit, req.cookies_browser) if req.expand else [src]
        except Exception as e:  # noqa: BLE001
            notes.append(f"Couldn't expand {src}: {core.friendly_error(e)}")
            found = []
        for u in found:
            if u not in urls:
                urls.append(u)
    urls = urls[:MAX_URLS_PER_REQUEST]
    if not urls:
        raise HTTPException(400, "\n".join(notes) or "Nothing to transcribe.")

    options = {
        "model": req.model, "language": req.language or None, "translate": req.translate,
        "save_video": req.save_video, "cookies_browser": req.cookies_browser or None,
    }
    out = []
    for u in urls:
        if not req.force:
            existing = db.find_done(u, req.model, req.language or None, req.translate, req.save_video)
            if existing:
                out.append({"id": existing, "url": u, "cached": True})
                continue
        jid = db.create(u, options, core.platform_of(u))
        _queue.put(jid)
        out.append({"id": jid, "url": u, "cached": False})
    return {"jobs": out, "notes": notes}


@app.get("/api/jobs")
def list_jobs(q: str | None = None, starred: bool = False):
    return {"jobs": db.list_jobs(q=q, starred=starred)}


def _job_or_404(jid: str) -> dict:
    job = db.get(jid)
    if not job:
        raise HTTPException(404, "No such job")
    return job


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    return _job_or_404(jid)


class JobPatch(BaseModel):
    starred: bool | None = None


@app.patch("/api/jobs/{jid}")
def patch_job(jid: str, patch: JobPatch):
    _job_or_404(jid)
    if patch.starred is not None:
        db.update(jid, starred=int(patch.starred))
    return {"ok": True}


@app.post("/api/jobs/{jid}/retry")
def retry_job(jid: str):
    job = _job_or_404(jid)
    if job["status"] not in ("error", "done"):
        raise HTTPException(409, "Job is still running")
    db.update(jid, status="queued", detail=None, error=None)
    _queue.put(jid)
    return {"ok": True}


@app.delete("/api/jobs/{jid}")
def delete_job(jid: str):
    job = _job_or_404(jid)
    if job["status"] in ("downloading", "transcribing"):
        raise HTTPException(409, "Job is running — wait for it to finish, then delete it.")
    db.delete(jid)
    return {"ok": True}


def _check_fmt(fmt: str) -> str:
    fmt = fmt.lower()
    if fmt not in core.FORMATS:
        raise HTTPException(400, f"Unknown format '{fmt}'. Choose from: {', '.join(core.FORMATS)}")
    return fmt


@app.get("/api/jobs/{jid}/export")
def export_job(jid: str, fmt: str = "txt", timestamps: bool = False):
    job = _job_or_404(jid)
    if job["status"] != "done":
        raise HTTPException(409, "Transcript isn't ready yet")
    fmt = _check_fmt(fmt)
    mime, ext = core.FORMATS[fmt]
    return Response(
        core.render(job, fmt, timestamps),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{core.export_name(job, ext)}"'},
    )


@app.get("/api/export.zip")
def export_zip(ids: str, fmt: str = "txt", timestamps: bool = False):
    fmt = _check_fmt(fmt)
    _mime, ext = core.FORMATS[fmt]
    buf = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for jid in [i for i in ids.split(",") if i]:
            job = db.get(jid)
            if not job or job["status"] != "done":
                continue
            name = core.export_name(job, ext)
            n = 1
            while name in used:  # disambiguate duplicates
                n += 1
                name = core.export_name(job, f"{n}.{ext}")
            used.add(name)
            z.writestr(name, core.render(job, fmt, timestamps))
    if not used:
        raise HTTPException(404, "None of the selected jobs have a finished transcript")
    return Response(
        buf.getvalue(), media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="transcripts.zip"'},
    )


def _media(jid: str, kind: str, download_name: str | None = None):
    job = _job_or_404(jid)
    fname = (job.get("files") or {}).get(kind)
    if not fname:
        raise HTTPException(404, f"No {kind} saved for this job")
    path = core.MEDIA_DIR / jid / Path(fname).name  # .name blocks path traversal
    if not path.exists():
        raise HTTPException(404, f"{kind} file is missing on disk")
    return FileResponse(path, filename=download_name)


@app.get("/api/jobs/{jid}/cover")
def get_cover(jid: str):
    return _media(jid, "cover")


@app.get("/api/jobs/{jid}/video")
def get_video(jid: str, download: bool = False):
    name = None
    if download:
        job = _job_or_404(jid)
        name = core.export_name(job, "mp4")
    return _media(jid, "video", name)


@app.exception_handler(HTTPException)
async def _http_exc(_req: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


def start_background(host: str = "127.0.0.1", port: int = 8765):
    """Serve in a daemon thread (desktop-app mode); it dies with the process. Returns (uvicorn server, thread)."""
    import uvicorn

    global _enforce_host_check
    _enforce_host_check = host in ("127.0.0.1", "localhost", "::1")
    srv = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True, name="localscript-server")
    thread.start()
    return srv, thread


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn

    global _enforce_host_check
    _enforce_host_check = host in ("127.0.0.1", "localhost", "::1")
    uvicorn.run(app, host=host, port=port, log_level="warning")
