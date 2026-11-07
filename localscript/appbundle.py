"""Create LocalScript.app: a small double-clickable launcher bundle for the desktop mode."""
from __future__ import annotations

import plistlib
import shutil
import stat
import subprocess
import sys
from pathlib import Path

BUNDLE_ID = "com.localscript.app"
ICNS = Path(__file__).parent / "static" / "LocalScript.icns"

LAUNCHER = """#!/bin/bash
# Launcher for {name}. Apps opened from Finder get a minimal PATH; ffmpeg and node live in Homebrew's.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
exec "{python}" -m localscript.cli app "$@"
"""


def build(dest: Path, name: str = "LocalScript", python: str | None = None) -> Path:
    """Write <dest>/<name>.app. The launcher runs `python -m localscript.cli app`, so `python` must be an
    interpreter that has LocalScript installed (default: the one running this command)."""
    python = python or sys.executable
    app = dest / f"{name}.app"
    plist_path = app / "Contents" / "Info.plist"

    if app.exists():  # only ever replace one of our own bundles
        try:
            ours = plistlib.loads(plist_path.read_bytes()).get("CFBundleIdentifier") == BUNDLE_ID
        except Exception:  # noqa: BLE001
            ours = False
        if not ours:
            raise RuntimeError(f"{app} exists and isn't a LocalScript app; not overwriting it.")
        shutil.rmtree(app)

    macos, resources = app / "Contents" / "MacOS", app / "Contents" / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    launcher = macos / name
    launcher.write_text(LAUNCHER.format(name=name, python=python))
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    if ICNS.exists():
        shutil.copy(ICNS, resources / "LocalScript.icns")

    plist = {
        "CFBundleName": name,
        "CFBundleDisplayName": name,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": name,
        "CFBundleIconFile": "LocalScript",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    }
    plist_path.write_bytes(plistlib.dumps(plist))

    # Ad-hoc signature gives the app a stable identity (best effort; the app runs without it).
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], capture_output=True)
    return app
