"""Desktop app mode: the server runs inside this process and the UI opens in a native window (WKWebView)."""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import db, server

APP_NAME = "LocalScript"
DEFAULT_PORT = 8765  # fixed, so the window keeps the same origin (and so its saved settings) between launches
ICON_PNG = Path(__file__).parent / "static" / "icon.png"


def _ensure_path() -> None:
    """Apps started from Finder get a bare PATH (/usr/bin:/bin:...). yt-dlp and Whisper need ffmpeg and node,
    which usually live in Homebrew's directories."""
    parts = os.environ.get("PATH", "").split(os.pathsep)
    extra = ["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local" / "bin")]
    os.environ["PATH"] = os.pathsep.join(parts + [p for p in extra if p not in parts and Path(p).is_dir()])


def _is_ours(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/api/health", timeout=1) as r:
            return json.load(r).get("app") == "localscript"
    except Exception:  # noqa: BLE001 - anything wrong just means "not ours / not up"
        return False


def _free_port(preferred: int) -> int:
    for port in (preferred, 0):
        with socket.socket() as s:
            # Same option uvicorn binds with: sockets lingering in TIME_WAIT after a quick quit and relaunch
            # must not make the preferred port look taken (the window's saved settings are tied to the port).
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("No free local port available")


def _brand() -> None:
    """Show 'LocalScript' (not 'Python') in the menu bar, and our icon in the Dock."""
    try:
        from AppKit import NSApplication, NSImage
        from Foundation import NSBundle

        info = NSBundle.mainBundle().infoDictionary()
        info["CFBundleName"] = APP_NAME
        info["CFBundleDisplayName"] = APP_NAME
        if ICON_PNG.exists():
            from PyObjCTools import AppHelper

            def set_icon() -> None:
                img = NSImage.alloc().initWithContentsOfFile_(str(ICON_PNG))
                if img is not None:
                    NSApplication.sharedApplication().setApplicationIconImage_(img)

            AppHelper.callAfter(set_icon)
    except Exception:  # noqa: BLE001 - cosmetic only
        pass


def _stay_awake_while_busy() -> None:
    """Hold a user-initiated activity assertion while jobs are unfinished, so macOS doesn't App-Nap a window that
    is behind other apps and slow a long batch. Released when the queue is empty (system idle sleep is still
    allowed)."""
    try:
        from Foundation import NSActivityUserInitiatedAllowingIdleSystemSleep, NSProcessInfo
    except Exception:  # noqa: BLE001 - not on macOS / no pyobjc
        return

    def watch() -> None:
        token = None
        while True:
            try:
                busy = bool(db.unfinished_ids())
                if busy and token is None:
                    token = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
                        NSActivityUserInitiatedAllowingIdleSystemSleep, "Transcribing videos")
                elif not busy and token is not None:
                    NSProcessInfo.processInfo().endActivity_(token)
                    token = None
            except Exception:  # noqa: BLE001 - never let the watcher die
                pass
            time.sleep(2)

    threading.Thread(target=watch, daemon=True, name="localscript-activity").start()


def run(port: int | None = None, debug: bool = False) -> int:
    _ensure_path()
    own_server = True
    if port is None:
        if _is_ours(DEFAULT_PORT):  # e.g. `localscript serve` is already running: just open a window on it
            port, own_server = DEFAULT_PORT, False
        else:
            port = _free_port(DEFAULT_PORT)
    if own_server:
        server.start_background("127.0.0.1", port)
        _stay_awake_while_busy()
        deadline = time.time() + 30
        while not _is_ours(port):
            if time.time() > deadline:
                print("LocalScript server didn't start in time.", file=sys.stderr)
                return 1
            time.sleep(0.15)
    url = f"http://localhost:{port}"

    try:
        import webview
    except ImportError:
        print(f"pywebview isn't installed, opening your browser instead: {url}", file=sys.stderr)
        webbrowser.open(url)
        if own_server:
            threading.Event().wait()  # keep serving until Ctrl+C
        return 0

    webview.settings["ALLOW_DOWNLOADS"] = True  # the export buttons download files
    window = webview.create_window(APP_NAME, url, width=760, height=620, min_size=(460, 440))

    def on_closing():
        n = len(db.unfinished_ids())
        if own_server and n:
            return bool(window.create_confirmation_dialog(
                APP_NAME,
                f"{n} video{' is' if n == 1 else 's are'} still being processed. Quit anyway?\n"
                "Unfinished videos start over the next time you open the app.",
            ))
        return True

    window.events.closing += on_closing
    _brand()
    webview.start(debug=debug)
    return 0
