"""Runs one job end to end: download -> audio -> transcribe -> persist."""
from __future__ import annotations

import shutil
import time
from typing import Callable

from . import core, db


def run_job(job_id: str, log: Callable[[str], None] | None = None) -> None:
    log = log or (lambda _m: None)
    job = db.get(job_id)
    if not job:
        return
    o = job["options"] or {}
    requested = o.get("model") or core.DEFAULT_MODEL
    translate = bool(o.get("translate"))
    want_video = bool(o.get("save_video"))
    jd = core.MEDIA_DIR / job_id
    t0 = time.time()
    try:
        db.update(job_id, status="downloading", detail="Fetching video…", error=None)
        log("downloading…")
        info, src = core.download(job["url"], jd, want_video, o.get("cookies_browser"))
        meta = core.meta_from_info(info)
        files: dict = {}
        cover = core.find_cover(jd)
        if cover:
            files["cover"] = cover.name
        # Save meta early so the UI can show title/cover while transcription runs.
        db.update(job_id, meta=meta, files=files)

        wav = jd / "audio.wav"
        core.to_wav(src, wav)
        if want_video:
            video = jd / ("video" + src.suffix)
            src.replace(video)
            files["video"] = video.name
        else:
            src.unlink(missing_ok=True)

        # "auto"/"parakeet-ultra" first need the language ID to pick an engine; say so while it runs.
        detecting = (requested in (core.AUTO, core.PARAKEET) and not translate
                     and not o.get("language") and core.parakeet_available())
        db.update(job_id, status="transcribing", files=files,
                  detail="Detecting language…" if detecting else "Transcribing…")
        engine_name, model, language, note = core.resolve_engine(requested, o.get("language"), translate, wav)
        if note:
            log(note)

        if core.model_cached(model):
            detail = "Transcribing…"
        elif engine_name == "parakeet":
            detail = "Downloading Parakeet Ultra — first run only (~740 MB)…"
        else:
            detail = f"Downloading Whisper '{model}' model — first run only ({core.MODEL_INFO[model].split(' ·')[0]})…"
        db.update(job_id, detail=detail)
        log(f"{detail.lower()} [{model}]")
        if engine_name == "parakeet":
            res = core.transcribe_parakeet(wav, language)
        else:
            res = core.transcribe(wav, model, language, translate, o.get("prompt"))
        wav.unlink(missing_ok=True)

        db.update(
            job_id,
            status="done", detail=None, error=None,
            text=res["text"], segments=res["segments"], language=res["language"], model=model,
            finished_at=time.time(), elapsed=round(time.time() - t0, 1),
        )
        log("done")
    except Exception as e:  # noqa: BLE001 - surface any failure to the user
        shutil.rmtree(jd, ignore_errors=True)
        db.update(job_id, status="error", detail=None, error=core.friendly_error(e),
                  files={}, finished_at=time.time(), elapsed=round(time.time() - t0, 1))
        log(f"failed: {core.friendly_error(e)}")
