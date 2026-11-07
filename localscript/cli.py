"""Command line interface:  localscript serve | transcribe | list"""
from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

from . import core, db, engine


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def cmd_serve(a: argparse.Namespace) -> int:
    from . import server

    url = f"http://{'localhost' if a.host in ('127.0.0.1', '::1') else a.host}:{a.port}"
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        _err(f"WARNING: binding to {a.host} exposes this tool (and downloads to your disk) to your network.")
    print(f"LocalScript running at {url}   (Ctrl+C to stop)", flush=True)
    if a.open:
        webbrowser.open(url)
    server.run(a.host, a.port)
    return 0


def cmd_transcribe(a: argparse.Namespace) -> int:
    db.init()
    sources: list[str] = []
    for raw in a.urls:
        sources += core.extract_urls(raw) or [raw]
    if a.file:
        sources += core.extract_urls(Path(a.file).read_text(encoding="utf-8"))
    if not sources:
        _err("No URLs given. Example: localscript transcribe https://www.tiktok.com/@user/video/123")
        return 2

    urls: list[str] = []
    for s in sources:
        try:
            found = core.expand_url(s, a.limit, a.cookies_from_browser) if not a.no_expand else [s]
        except Exception as e:  # noqa: BLE001
            _err(f"Couldn't expand {s}: {core.friendly_error(e)}")
            found = [s]
        urls += [u for u in found if u not in urls]

    formats = [f.strip().lower() for f in a.format.split(",") if f.strip()]
    for f in formats:
        if f not in core.FORMATS:
            _err(f"Unknown format '{f}'. Choose from: {', '.join(core.FORMATS)}")
            return 2
    out_dir = Path(a.out).expanduser() if a.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    options = {
        "model": a.model, "language": a.language, "translate": a.translate,
        "save_video": bool(a.video and out_dir), "cookies_browser": a.cookies_from_browser,
    }
    failures = 0
    for i, url in enumerate(urls, 1):
        _err(f"[{i}/{len(urls)}] {url}")
        jid = db.create(url, options, core.platform_of(url))
        engine.run_job(jid, log=lambda m: _err(f"    {m}"))
        job = db.get(jid)
        if job["status"] != "done":
            failures += 1
            _err(f"    ERROR: {job['error']}")
            continue
        if not job["text"]:
            _err("    (no speech detected)")
        if out_dir:
            for f in formats:
                p = out_dir / core.export_name(job, core.FORMATS[f][1])
                p.write_text(core.render(job, f, a.timestamps), encoding="utf-8")
                _err(f"    wrote {p}")
            if a.video and (job["files"] or {}).get("video"):
                src = core.MEDIA_DIR / jid / job["files"]["video"]
                dst = out_dir / core.export_name(job, "mp4")
                dst.write_bytes(src.read_bytes())
                _err(f"    wrote {dst}")
        else:
            if len(urls) > 1:
                print(f"# {job['meta'].get('title')} — {url}")
            print(core.render(job, formats[0], a.timestamps), end="" if len(urls) == 1 else "\n")
    return 1 if failures else 0


def cmd_list(a: argparse.Namespace) -> int:
    db.init()
    for j in db.list_jobs(q=a.query, limit=a.n):
        title = (j["meta"] or {}).get("title") or j["url"]
        star = "★" if j["starred"] else " "
        print(f"{j['id']}  {j['status']:<12} {star} {title[:70]}")
    return 0


def cmd_app(a: argparse.Namespace) -> int:
    from . import app

    return app.run(a.port, a.debug)


def cmd_install_app(a: argparse.Namespace) -> int:
    from . import appbundle

    dest = Path(a.dest).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    try:
        path = appbundle.build(dest, a.name)
    except RuntimeError as e:
        _err(str(e))
        return 1
    print(f"Created {path}")
    print(f"It runs with: {sys.executable}")
    print("Open it from Finder, or drag it to the Dock. Re-run this command after moving or reinstalling the tool.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="localscript",
        description="Turn speech from TikTok, Instagram Reels and YouTube/Shorts videos into text — locally.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="start the local web app")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--open", action="store_true", help="open the browser")
    s.set_defaults(fn=cmd_serve)

    t = sub.add_parser("transcribe", help="transcribe one or more videos / profiles / playlists")
    t.add_argument("urls", nargs="*", help="video, profile or playlist URLs")
    t.add_argument("-f", "--file", help="text file containing URLs")
    t.add_argument("-m", "--model", default=core.DEFAULT_MODEL, choices=core.ALL_MODEL_IDS,
                   help="auto = Parakeet for European languages, Whisper for the rest (default); "
                        "parakeet-ultra; or a Whisper size")
    t.add_argument("-l", "--language", help="spoken language code, e.g. en, es, pt (default: auto-detect)")
    t.add_argument("--translate", action="store_true", help="translate speech into English")
    t.add_argument("--format", default="txt", help=f"comma-separated: {','.join(core.FORMATS)} (default: txt)")
    t.add_argument("--timestamps", action="store_true", help="include timestamps in txt output")
    t.add_argument("-o", "--out", help="write files to this folder instead of printing")
    t.add_argument("--video", action="store_true", help="also save the HD video (needs --out)")
    t.add_argument("--limit", type=int, default=50, help="max videos taken from a profile/playlist (default 50)")
    t.add_argument("--no-expand", action="store_true", help="treat every URL as a single video")
    t.add_argument("--cookies-from-browser", choices=sorted(core.COOKIE_BROWSERS),
                   help="use a browser's logged-in cookies (needed for some Instagram / private content)")
    t.set_defaults(fn=cmd_transcribe)

    l = sub.add_parser("list", help="show recent history")
    l.add_argument("-q", "--query")
    l.add_argument("-n", type=int, default=20)
    l.set_defaults(fn=cmd_list)

    ap = sub.add_parser("app", help="open the desktop app window (server runs inside it)")
    ap.add_argument("--port", type=int, help="port for the embedded server (default: 8765 if free)")
    ap.add_argument("--debug", action="store_true", help="enable the web inspector")
    ap.set_defaults(fn=cmd_app)

    ia = sub.add_parser("install-app", help="create the double-clickable LocalScript.app")
    ia.add_argument("--dest", default="~/Applications", help="folder to put it in (default: ~/Applications)")
    ia.add_argument("--name", default="LocalScript", help="app name (default: LocalScript)")
    ia.set_defaults(fn=cmd_install_app)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        _err("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
