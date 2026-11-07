"""SQLite-backed job history (shared by the CLI and the web UI)."""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
import uuid

from . import core

JSON_COLS = {"options": {}, "meta": {}, "segments": [], "files": {}}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id          TEXT PRIMARY KEY,
  url         TEXT NOT NULL,
  platform    TEXT,
  status      TEXT NOT NULL,          -- queued | downloading | transcribing | done | error
  detail      TEXT,                   -- human-readable stage detail
  error       TEXT,
  options     TEXT,                   -- json: model, language, translate, save_video, ...
  meta        TEXT,                   -- json: title, uploader, duration, ...
  language    TEXT,
  model       TEXT,
  text        TEXT,
  segments    TEXT,                   -- json: [{start,end,text}]
  files       TEXT,                   -- json: {video, cover}
  starred     INTEGER NOT NULL DEFAULT 0,
  created_at  REAL NOT NULL,
  finished_at REAL,
  elapsed     REAL
);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created_at DESC);
"""

LIST_COLS = (
    "id, url, platform, status, detail, error, options, meta, language, model, files, starred, "
    "created_at, finished_at, elapsed, substr(text, 1, 220) AS preview, length(text) AS text_len"
)


def _conn() -> sqlite3.Connection:
    core.HOME.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(core.DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init() -> None:
    with _conn() as c:
        c.executescript(SCHEMA)


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k, default in JSON_COLS.items():
        if k in d:
            d[k] = json.loads(d[k]) if d[k] else (type(default)())
    if "starred" in d:
        d["starred"] = bool(d["starred"])
    return d


def create(url: str, options: dict, platform: str) -> str:
    jid = uuid.uuid4().hex[:12]
    with _conn() as c:
        c.execute(
            "INSERT INTO jobs (id, url, platform, status, options, created_at) VALUES (?,?,?,?,?,?)",
            (jid, url, platform, "queued", json.dumps(options), time.time()),
        )
    return jid


def update(jid: str, **fields) -> None:
    if not fields:
        return
    vals = []
    for k, v in fields.items():
        vals.append(json.dumps(v, ensure_ascii=False) if k in JSON_COLS and v is not None else v)
    sets = ", ".join(f"{k}=?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*vals, jid))


def get(jid: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    return _row(r) if r else None


def list_jobs(q: str | None = None, starred: bool = False, limit: int = 300) -> list[dict]:
    where, args = [], []
    if starred:
        where.append("starred=1")
    if q:
        like = f"%{q}%"
        where.append("(text LIKE ? OR url LIKE ? OR json_extract(meta,'$.title') LIKE ? "
                     "OR json_extract(meta,'$.uploader') LIKE ?)")
        args += [like] * 4
    sql = f"SELECT {LIST_COLS} FROM jobs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    with _conn() as c:
        rows = c.execute(sql, (*args, limit)).fetchall()
    return [_row(r) for r in rows]


def delete(jid: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM jobs WHERE id=?", (jid,))
    shutil.rmtree(core.MEDIA_DIR / jid, ignore_errors=True)


def find_done(url: str, model: str, language: str | None, translate: bool, need_video: bool = False) -> str | None:
    """Return the id of an existing finished job for the same URL + settings (acts as a cache)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, options, files FROM jobs WHERE url=? AND status='done' ORDER BY created_at DESC", (url,)
        ).fetchall()
    for r in rows:
        o = json.loads(r["options"] or "{}")
        if need_video and not (json.loads(r["files"] or "{}")).get("video"):
            continue  # caller wants the HD video and this earlier run didn't keep it
        if (o.get("model") == model and (o.get("language") or None) == (language or None)
                and bool(o.get("translate")) == translate):
            return r["id"]
    return None


def unfinished_ids() -> list[str]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id FROM jobs WHERE status IN ('queued','downloading','transcribing') ORDER BY created_at"
        ).fetchall()
    return [r["id"] for r in rows]
