"""Core pipeline: URL handling -> yt-dlp download -> ffmpeg audio -> Whisper -> exports.

Everything here runs locally. No video, audio or text is sent to any third party
(other than the platform you are downloading the video from, and Hugging Face the
first time a Whisper model is fetched).
"""
from __future__ import annotations

import atexit
import contextlib
import csv
import io
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import quoteattr

HOME = Path(os.environ.get("LOCALSCRIPT_HOME", "~/.localscript")).expanduser()
MEDIA_DIR = HOME / "media"
DB_PATH = HOME / "history.db"

# --------------------------------------------------------------------------- models

MODEL_REPOS = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
}
MODEL_INFO = {
    "tiny": "~75 MB · fastest, rough accuracy",
    "base": "~140 MB · fast",
    "small": "~460 MB · good balance",
    "medium": "~1.5 GB · accurate",
    "large-v3-turbo": "~1.6 GB · best Whisper speed/accuracy",
    "large-v3": "~3 GB · most accurate Whisper, slowest, can translate",
}
WHISPER_DEFAULT = "large-v3-turbo"

# Non-Whisper engine: Moondream's post-trained NVIDIA Parakeet v3 (FastConformer + TDT transducer), run on
# Metal through transcribe.cpp. Fast and small, but only 25 European languages and no translation.
PARAKEET = "parakeet-ultra"
AUTO = "auto"  # Parakeet when the speech is one of its languages, Whisper otherwise
DEFAULT_MODEL = AUTO
ALL_MODEL_IDS = [AUTO, PARAKEET, *MODEL_REPOS]
PARAKEET_REPO = "handy-computer/parakeet-ultra-gguf"
PARAKEET_FILE = "parakeet-ultra-Q8_0.gguf"
PARAKEET_INFO = "~740 MB · European languages only, fastest"
PARAKEET_LANGS = frozenset(
    "en de fr es it pt ru uk hr sl lv lt et fi sv da nl pl cs sk hu ro bg el mt".split()
)
ENGINE_DIR = Path(os.environ.get("LOCALSCRIPT_TRANSCRIBE_CPP", HOME / "engines" / "transcribe.cpp")).expanduser()


def is_valid_model(name: str) -> bool:
    return name in ALL_MODEL_IDS

COOKIE_BROWSERS = {"chrome", "safari", "firefox", "brave", "edge", "chromium", "opera", "vivaldi"}
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}


def effective_model(model: str | None, translate: bool) -> str:
    """large-v3-turbo was trained for transcription only: asked to translate, it returns empty or
    garbled text. Fall back to full large-v3 for translation."""
    model = model or WHISPER_DEFAULT
    return "large-v3" if translate and model == "large-v3-turbo" else model


def model_cached(name: str) -> bool:
    """True when the model weights are already in the local Hugging Face cache."""
    if name == PARAKEET:
        return parakeet_available() and parakeet_model_path() is not None
    if name == AUTO:
        return model_cached(PARAKEET if parakeet_available() else WHISPER_DEFAULT)
    try:
        from huggingface_hub import try_to_load_from_cache

        repo = MODEL_REPOS[name]
        for fname in ("weights.safetensors", "weights.npz"):
            p = try_to_load_from_cache(repo, fname)
            if isinstance(p, str):
                return True
    except Exception:
        pass
    return False


# ------------------------------------------------------------------------------ urls

KNOWN_HOSTS = (
    "tiktok.com", "instagram.com", "youtube.com", "youtu.be",
    "facebook.com", "fb.watch", "x.com", "twitter.com",
)


def extract_urls(text: str) -> list[str]:
    """Pull video URLs out of pasted text (newline/space/comma separated). Dedupes, keeps order."""
    out: list[str] = []
    for tok in re.split(r"[\s,]+", (text or "").strip()):
        # strip wrapping/trailing punctuation from chat or markdown, e.g. "(https://…)." or "<https://…>"
        tok = re.sub(r"^[<(\[\"']+|[>)\]\"'.,;!?]+$", "", tok)
        if not tok:
            continue
        if re.match(r"https?://", tok, re.I):
            url = tok
        elif any(re.match(rf"^(?:[\w-]+\.)*{re.escape(h)}/", tok, re.I) for h in KNOWN_HOSTS):
            url = "https://" + tok
        else:
            continue
        if url not in out:
            out.append(url)
    return out


def platform_of(url: str) -> str:
    u = urlparse(url)
    host, path = u.netloc.lower(), u.path.lower()
    if "tiktok.com" in host:
        return "tiktok"
    if "instagram.com" in host:
        return "instagram"
    if "youtu.be" in host or "youtube.com" in host:
        return "shorts" if "/shorts/" in path else "youtube"
    if "facebook.com" in host or "fb.watch" in host:
        return "facebook"
    if host in ("x.com", "twitter.com") or host.endswith((".x.com", ".twitter.com")):
        return "x"
    return "other"


def looks_like_collection(url: str) -> bool:
    """Heuristic: is this a profile / playlist / collection rather than a single video?"""
    u = urlparse(url)
    host, path = u.netloc.lower(), u.path
    if "tiktok.com" in host:
        if re.search(r"/(video|photo)/\d+", path) or host.startswith(("vm.", "vt.")) or path.startswith("/t/"):
            return False
        return "/collection/" in path or "/playlist/" in path or bool(re.fullmatch(r"/@[^/]+/?", path))
    if "youtube.com" in host:
        if path.startswith("/playlist"):
            return True
        return bool(re.fullmatch(r"/(@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)(/(videos|shorts|streams))?/?", path))
    if "instagram.com" in host:
        m = re.fullmatch(r"/([^/]+)(/reels)?/?", path)
        return bool(m) and m.group(1) not in {"reel", "reels", "p", "tv", "stories", "explore", "accounts"}
    return False


# ------------------------------------------------------------------------- yt-dlp

class _SilentLogger:
    """yt-dlp prints errors to stderr even in quiet mode; we surface them via exceptions instead."""
    def debug(self, _m): pass
    def info(self, _m): pass
    def warning(self, _m): pass
    def error(self, _m): pass


def _ydl_base(cookies_browser: str | None = None) -> dict:
    opts: dict = {
        "logger": _SilentLogger(),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "retries": 3,
        "socket_timeout": 30,
        # yt-dlp needs a JS runtime to solve YouTube challenges; node is the common one.
        "js_runtimes": {"node": {}},
    }
    cb = cookies_browser or os.environ.get("LOCALSCRIPT_COOKIES_BROWSER")
    if cb:
        if cb not in COOKIE_BROWSERS:
            raise ValueError(f"Unsupported cookie browser: {cb}")
        opts["cookiesfrombrowser"] = (cb,)
    return opts


def expand_url(url: str, limit: int = 50, cookies_browser: str | None = None) -> list[str]:
    """Turn a profile/playlist/collection URL into its video URLs (capped). Single videos pass through."""
    if not looks_like_collection(url):
        return [url]
    import yt_dlp

    opts = _ydl_base(cookies_browser) | {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "playlistend": max(1, limit),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = [e for e in (info or {}).get("entries") or [] if e]
    urls = []
    for e in entries[:limit]:
        u = e.get("webpage_url") or e.get("url")
        if u and u.startswith("http") and u not in urls:
            urls.append(u)
        elif e.get("id") and "youtube" in (e.get("ie_key") or "").lower():
            urls.append(f"https://www.youtube.com/watch?v={e['id']}")
    return urls or [url]


def download(url: str, job_dir: Path, want_video: bool = False, cookies_browser: str | None = None):
    """Download media (+cover). Returns (info, source_path)."""
    import yt_dlp

    job_dir.mkdir(parents=True, exist_ok=True)
    opts = _ydl_base(cookies_browser) | {
        "outtmpl": str(job_dir / "source.%(ext)s"),
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b" if want_video else "ba/b",
        "merge_output_format": "mp4",
        "writethumbnail": True,
        "postprocessors": [{"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"}],
    }
    if want_video:
        # Prefer H.264/AAC (plays everywhere) over HEVC/AV1/VP9, then highest resolution. Not applied to the
        # audio-only path: that keeps yt-dlp's default (best) audio stream, which is what was validated.
        opts["format_sort"] = ["vcodec:h264", "res", "acodec:aac"]
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    if info and info.get("entries"):
        info = next((e for e in info["entries"] if e), info)

    path = None
    for rd in (info or {}).get("requested_downloads") or []:
        if rd.get("filepath") and Path(rd["filepath"]).exists():
            path = Path(rd["filepath"])
            break
    if path is None:
        cands = [p for p in job_dir.glob("source.*") if p.suffix.lower() not in IMG_EXT]
        if not cands:
            raise RuntimeError("Download finished but no media file was produced.")
        path = max(cands, key=lambda p: p.stat().st_size)
    return info, path


def find_cover(job_dir: Path) -> Path | None:
    """Locate the thumbnail yt-dlp wrote and normalise it to cover.jpg."""
    imgs = [p for p in job_dir.glob("source.*") if p.suffix.lower() in IMG_EXT]
    if not imgs:
        return None
    imgs.sort(key=lambda p: (p.suffix.lower() != ".jpg", -p.stat().st_size))
    dst = job_dir / ("cover" + imgs[0].suffix.lower())
    imgs[0].replace(dst)
    for extra in imgs[1:]:
        extra.unlink(missing_ok=True)
    return dst


def meta_from_info(info: dict) -> dict:
    info = info or {}
    desc = info.get("description") or ""
    title = info.get("title") or desc.strip().split("\n")[0][:100] or info.get("id") or "Untitled"
    return {
        "id": info.get("id"),
        "title": title,
        "description": desc,
        "uploader": info.get("uploader") or info.get("channel") or info.get("creator"),
        "uploader_id": info.get("uploader_id") or info.get("channel_id"),
        "duration": info.get("duration"),
        "upload_date": info.get("upload_date"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "repost_count": info.get("repost_count"),
        "tags": info.get("tags") or [],
        "webpage_url": info.get("webpage_url") or info.get("original_url"),
        "extractor": info.get("extractor_key") or info.get("extractor"),
    }


# ----------------------------------------------------------------- audio + whisper

def to_wav(src: Path, dst: Path) -> None:
    """Decode any media file to 16 kHz mono PCM, the format Whisper wants."""
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(dst)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size < 1000:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or ["unknown ffmpeg error"]
        raise RuntimeError(f"Could not extract audio (does the video have a sound track?): {tail[0]}")


_tx_lock = threading.Lock()  # one transcription at a time keeps the GPU/RAM happy

# Whisper invents these on music/silence (its training data is full of subtitle outros).
_FILLER = {
    "thanks for watching", "thank you for watching", "thank you", "please subscribe",
    "like and subscribe", "subtitles by the amaraorg community", "bye", "you",
}


def _is_hallucination(seg: dict) -> bool:
    """Heuristics for segments Whisper made up over music / silence / noise.

    no_speech_prob alone is unreliable (large-v3-turbo reports ~0 even on garbage), so lean on
    avg_logprob: real speech sits around -0.2..-0.9, invented text is far lower.
    """
    lp = seg.get("avg_logprob", 0.0)
    nsp = seg.get("no_speech_prob", 0.0)
    if lp < -2.0:
        return True
    if nsp > 0.6 and lp < -1.0:
        return True
    norm = re.sub(r"[^\w\s]", "", seg["text"].lower()).strip()
    return lp < -0.9 and norm in _FILLER


def transcribe(wav: Path, model: str = WHISPER_DEFAULT, language: str | None = None,
               translate: bool = False, prompt: str | None = None) -> dict:
    """Whisper transcription (or translation to English)."""
    import mlx_whisper

    if model not in MODEL_REPOS:
        raise ValueError(f"Unknown model '{model}'. Choose from: {', '.join(MODEL_REPOS)}")
    with _tx_lock:
        res = mlx_whisper.transcribe(
            str(wav),
            path_or_hf_repo=MODEL_REPOS[model],
            language=language or None,
            task="translate" if translate else "transcribe",
            condition_on_previous_text=False,  # avoids repetition loops on short, noisy clips
            initial_prompt=prompt or None,
            word_timestamps=False,
            verbose=None,
        )
    segments = []
    for s in res.get("segments", []):
        text = s["text"].strip()
        if not text or _is_hallucination(s):
            continue
        segments.append({"start": round(float(s["start"]), 2), "end": round(float(s["end"]), 2), "text": text})
    return {
        "text": " ".join(s["text"] for s in segments),
        "segments": segments,
        "language": res.get("language"),
    }


# ------------------------------------------------------- Parakeet (via transcribe.cpp)

@contextlib.contextmanager
def _quiet_stderr():
    """ggml prints Metal setup and per-run stats straight to file descriptor 2; hide that while we call it.
    Only ever used while holding _tx_lock, so at most a stray log line from another thread is lost."""
    try:
        sys.stderr.flush()
        saved = os.dup(2)
    except OSError:
        yield
        return
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 2)
    os.close(devnull)
    try:
        yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)


def _parakeet_paths() -> tuple[Path, Path]:
    suffix = ".dylib" if sys.platform == "darwin" else ".so"
    return (ENGINE_DIR / "build-shared" / "src" / f"libtranscribe{suffix}",
            ENGINE_DIR / "bindings" / "python" / "src")


def parakeet_available() -> bool:
    """True once scripts/install_parakeet.sh has built transcribe.cpp."""
    lib, binding = _parakeet_paths()
    return lib.exists() and binding.exists()


def parakeet_model_path(download: bool = False) -> str | None:
    """Path of the Parakeet Ultra GGUF in the Hugging Face cache (Handy uses the same cache)."""
    from huggingface_hub import hf_hub_download, try_to_load_from_cache

    p = try_to_load_from_cache(PARAKEET_REPO, PARAKEET_FILE)
    if isinstance(p, str):
        return p
    return hf_hub_download(PARAKEET_REPO, PARAKEET_FILE) if download else None


_parakeet = None  # (transcribe_cpp module, loaded Model), created on first use


def _load_parakeet():
    global _parakeet
    if _parakeet is not None:
        return _parakeet
    if not parakeet_available():
        raise RuntimeError("Parakeet isn't installed. Run scripts/install_parakeet.sh (see the README).")
    lib, binding = _parakeet_paths()
    gguf = parakeet_model_path(download=True)
    os.environ.setdefault("TRANSCRIBE_LIBRARY", str(lib))
    if str(binding) not in sys.path:
        sys.path.insert(0, str(binding))
    with _quiet_stderr():
        import transcribe_cpp

        transcribe_cpp.set_log_callback(None)
        model = transcribe_cpp.Model(gguf)
    _parakeet = (transcribe_cpp, model)
    atexit.register(_close_parakeet)
    return _parakeet


def _close_parakeet() -> None:
    """Free the Parakeet model's Metal buffers before the interpreter exits. ggml asserts, and aborts the whole
    process (a crash report on every quit), if its Metal device is torn down while model buffers are still
    alive, which happens when daemon server threads keep Python from freeing the model during shutdown."""
    global _parakeet
    if _parakeet is None or not _tx_lock.acquire(timeout=3):  # don't pull the model out from under a running job
        return
    try:
        _tc, model = _parakeet
        _parakeet = None
        with _quiet_stderr():
            model.close()
    except Exception:  # noqa: BLE001 - exiting anyway
        pass
    finally:
        _tx_lock.release()


def _read_wav_f32(wav: Path):
    """Read the 16 kHz mono PCM16 file made by to_wav() as float32 in [-1, 1]."""
    import wave

    import numpy as np

    with wave.open(str(wav), "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
            raise RuntimeError("Expected 16 kHz mono 16-bit audio")
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


_SENTENCE_END = re.compile(r"[.!?…][\"')\]]*$")


def _cues_from_words(words, max_dur: float = 10.0, max_chars: int = 160, max_gap: float = 1.0) -> list[dict]:
    """Group Parakeet's word timestamps into subtitle-sized cues. The model's own segments can run to
    30 s, which is too long for captions: break at sentence ends, pauses, and a duration/length cap."""
    cues: list[dict] = []
    cur: list = []

    def flush() -> None:
        if cur:
            cues.append({
                "start": round(cur[0].t0_ms / 1000, 2),
                "end": round(cur[-1].t1_ms / 1000, 2),
                "text": " ".join(w.text for w in cur).strip(),
            })
            cur.clear()

    for w in words:
        if cur:
            gap = (w.t0_ms - cur[-1].t1_ms) / 1000
            dur = (w.t1_ms - cur[0].t0_ms) / 1000
            chars = sum(len(x.text) + 1 for x in cur)
            if gap >= max_gap or dur > max_dur or chars > max_chars:
                flush()
        cur.append(w)
        if _SENTENCE_END.search(w.text):
            flush()
    flush()
    return [c for c in cues if c["text"]]


def transcribe_parakeet(wav: Path, language: str | None = None) -> dict:
    """Transcribe with Parakeet Ultra. `language` is only a hint; it must be one of PARAKEET_LANGS."""
    pcm = _read_wav_f32(wav)
    with _tx_lock:
        _tc, model = _load_parakeet()
        with _quiet_stderr(), model.session() as sess:
            r = sess.run(pcm, language=language if language in PARAKEET_LANGS else None, timestamps="word")
    segments = _cues_from_words(r.words)
    if not segments:  # no word timings: fall back to the model's own segments
        segments = [{"start": round(s.t0_ms / 1000, 2), "end": round(s.t1_ms / 1000, 2), "text": s.text.strip()}
                    for s in r.segments if s.text.strip()]
    return {
        "text": " ".join(s["text"] for s in segments),
        "segments": segments,
        "language": language or (r.language or None),
    }


# ------------------------------------------------------------ language ID + engine choice

_langid_model = None
# Whisper tiny scored 98-99% on clear speech and 30-40% on music-heavy clips (one of them Korean, which it
# called English). Below this we don't trust it enough to hand a clip to a European-only engine.
LANGID_MIN_CONFIDENCE = 0.8


def detect_language(wav: Path) -> tuple[str, float]:
    """Spoken-language ID with Whisper tiny on the first 30 s: under a second, ~99 languages.
    (Parakeet's own detection only chooses among its 25 languages, so it can't tell us when to
    hand a clip to Whisper.)"""
    global _langid_model
    import mlx.core as mx
    from mlx_whisper.audio import N_FRAMES, N_SAMPLES, log_mel_spectrogram, pad_or_trim
    from mlx_whisper.load_models import load_model

    with _tx_lock:
        if _langid_model is None:
            _langid_model = load_model(MODEL_REPOS["tiny"], dtype=mx.float16)
        pcm = _read_wav_f32(wav)[: 30 * 16000]
        mel = log_mel_spectrogram(pcm, n_mels=_langid_model.dims.n_mels, padding=N_SAMPLES)
        segment = pad_or_trim(mel, N_FRAMES, axis=-2).astype(mx.float16)
        _, probs = _langid_model.detect_language(segment)
    if isinstance(probs, list):
        probs = probs[0]
    lang = max(probs, key=probs.get)
    return lang, float(probs[lang])


def warm_up() -> None:
    """Load the language-ID and Parakeet models and compile their GPU kernels on two seconds of silence, so the
    first real job doesn't pay a 10-30 s cold start. Best effort: failures are ignored (a real job reports them)."""
    import tempfile
    import wave

    try:
        if not parakeet_available():
            return
        with tempfile.TemporaryDirectory() as d:
            wav = Path(d) / "silence.wav"
            with wave.open(str(wav), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"\x00\x00" * 32000)
            detect_language(wav)
            transcribe_parakeet(wav, None)
    except Exception:  # noqa: BLE001
        pass


def resolve_engine(model: str | None, language: str | None, translate: bool, wav: Path):
    """Decide which engine runs a job. Returns (engine, model_name, language, note).

    engine is "parakeet" or "whisper"; note explains any deviation from what was asked, or is None.
      * Translation always needs Whisper (Parakeet can't translate; turbo can't either, so large-v3).
      * An explicit Whisper model is honoured as-is.
      * "auto" / "parakeet-ultra" use Parakeet only when the speech is one of its 25 languages and the
        language ID is reasonably sure; anything else falls back to Whisper.
    """
    model = model or DEFAULT_MODEL
    if translate:
        base = WHISPER_DEFAULT if model in (AUTO, PARAKEET) else model
        return "whisper", effective_model(base, True), language, None
    if model in MODEL_REPOS:
        return "whisper", model, language, None
    if not parakeet_available():
        note = "Parakeet isn't installed, using Whisper" if model == PARAKEET else None
        return "whisper", WHISPER_DEFAULT, language, note
    lang, p = (language, 1.0) if language else detect_language(wav)
    if lang in PARAKEET_LANGS and p >= LANGID_MIN_CONFIDENCE:
        return "parakeet", PARAKEET, lang, None
    why = f"language '{lang}' isn't covered by Parakeet" if lang not in PARAKEET_LANGS \
        else f"language ID unsure ({lang} {p:.0%})"
    return "whisper", WHISPER_DEFAULT, language, f"{why}, using Whisper"


def model_catalog() -> list[dict]:
    """Everything the UI's model picker offers, with install/cache state."""
    have = parakeet_available()
    out = [
        {"id": AUTO, "available": True, "cached": model_cached(AUTO),
         "info": ("Parakeet for European languages, Whisper for the rest (recommended)" if have
                  else "Whisper large-v3-turbo (set up Parakeet for faster European-language runs)")},
        {"id": PARAKEET, "available": have, "cached": model_cached(PARAKEET),
         "info": PARAKEET_INFO if have else "not set up, see README (scripts/install_parakeet.sh)"},
    ]
    out += [{"id": m, "available": True, "cached": model_cached(m), "info": MODEL_INFO[m]} for m in MODEL_REPOS]
    return out


# ----------------------------------------------------------------------- exports

FORMATS = {
    "txt": ("text/plain; charset=utf-8", "txt"),
    "srt": ("application/x-subrip; charset=utf-8", "srt"),
    "vtt": ("text/vtt; charset=utf-8", "vtt"),
    "json": ("application/json; charset=utf-8", "json"),
    "csv": ("text/csv; charset=utf-8", "csv"),
    "xml": ("application/xml; charset=utf-8", "xml"),
}


def _ts(sec: float, sep: str) -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _short_ts(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def render(job: dict, fmt: str, timestamps: bool = False) -> str:
    fmt = fmt.lower()
    segs = job.get("segments") or []
    meta = job.get("meta") or {}
    if fmt == "txt":
        if timestamps:
            return "\n".join(f"[{_short_ts(s['start'])}] {s['text']}" for s in segs) + "\n"
        return (job.get("text") or "") + "\n"
    if fmt == "srt":
        return "\n".join(
            f"{i}\n{_ts(s['start'], ',')} --> {_ts(s['end'], ',')}\n{s['text']}\n" for i, s in enumerate(segs, 1)
        )
    if fmt == "vtt":
        body = "\n".join(f"{_ts(s['start'], '.')} --> {_ts(s['end'], '.')}\n{s['text']}\n" for s in segs)
        return "WEBVTT\n\n" + body
    if fmt == "json":
        return json.dumps(
            {
                "url": job.get("url"),
                "meta": meta,
                "language": job.get("language"),
                "model": job.get("model"),
                "translated": bool((job.get("options") or {}).get("translate")),
                "text": job.get("text") or "",
                "segments": segs,
            },
            ensure_ascii=False, indent=2,
        ) + "\n"
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["start", "end", "text"])
        for s in segs:
            w.writerow([s["start"], s["end"], s["text"]])
        return buf.getvalue()
    if fmt == "xml":
        attrs = " ".join(
            f"{k}={quoteattr(str(v))}"
            for k, v in (
                ("url", job.get("url")), ("title", meta.get("title")), ("author", meta.get("uploader")),
                ("language", job.get("language")), ("model", job.get("model")),
            ) if v
        )
        rows = "\n".join(
            f'  <segment start="{s["start"]}" end="{s["end"]}">{xml_escape(s["text"])}</segment>' for s in segs
        )
        return f'<?xml version="1.0" encoding="UTF-8"?>\n<transcript {attrs}>\n{rows}\n</transcript>\n'
    raise ValueError(f"Unknown format '{fmt}'. Choose from: {', '.join(FORMATS)}")


def export_name(job: dict, ext: str) -> str:
    meta = job.get("meta") or {}
    parts = [meta.get("uploader") or "", meta.get("id") or job.get("id") or "transcript"]
    slug = re.sub(r"[^\w.-]+", "_", "_".join(p for p in parts if p), flags=re.UNICODE).strip("_")
    return f"{slug[:80] or 'transcript'}.{ext}"


# ------------------------------------------------------------------------- errors

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def friendly_error(e: Exception) -> str:
    msg = _ANSI.sub("", str(e)).strip()
    msg = re.sub(r"^ERROR:\s*", "", msg)
    msg = re.sub(r"[;,]?\s*(?:please report this issue|See\s+https?://)\S*.*$", "", msg, flags=re.S).strip()
    low = msg.lower()
    if "tiktok" in low and ("failed to parse json" in low or "unexpected response" in low):
        return ("TikTok blocked this request (it served a bot check instead of data). Single video links "
                "usually work; for profiles and collections pick your browser under \"Sign-in\" (while logged "
                "in to TikTok there), or retry in a few minutes.")
    hint = ""
    if any(k in low for k in ("login required", "log in", "cookies", "rate-limit", "sign in", "private")):
        hint = (" — Tip: this platform wants a signed-in session. Pick your browser under \"Sign-in\" "
                "(CLI: --cookies-from-browser chrome) while logged in there, then retry.")
    return (msg + hint)[:700]
