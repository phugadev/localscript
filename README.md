# LocalScript

A local rebuild of [TokScript](https://tokscript.com): paste a TikTok, Instagram Reel or YouTube / Shorts link and get the speech as text. Everything runs on your Mac. Videos are downloaded with **yt-dlp** and transcribed with **Whisper** (via `mlx-whisper`, which uses the Apple Silicon GPU) or, optionally, **Parakeet Ultra** (via `transcribe.cpp` on Metal, faster for English and 24 other European languages). No account, no credits, no upload.

## Quick start

Needs [`uv`](https://docs.astral.sh/uv/), `ffmpeg` and `node` (all via Homebrew if missing).

```bash
./run.sh            # installs on first run, starts the app, opens http://localhost:8765
```

The first transcription downloads the Whisper model once (`large-v3-turbo`, ~1.6 GB). After that it works offline except for fetching the videos themselves.

## Desktop app (macOS)

**`~/Applications/LocalScript.app`** is a native window with its own Dock icon (drag it to the Dock if you like). The server runs inside it, so there's no terminal to keep open. It shares its history and settings with `./run.sh` and the command line. If a LocalScript server is already running, the window just attaches to it. Quitting while videos are still processing asks first; unfinished ones restart on the next launch. Export buttons open a normal Save dialog. At launch it warms the Parakeet engine in the background so the first video isn't slow.

The app runs an *installed copy* of the code, not this folder. Set it up, and update it after changing the code, with:

```bash
uv tool install --python 3.12 --reinstall .
localscript install-app          # creates ~/Applications/LocalScript.app (--name / --dest to change)
```

`localscript app` opens the same window from a terminal. The launcher needs the Homebrew tools `ffmpeg` and `node`; it adds `/opt/homebrew/bin` to the PATH itself, since apps opened from Finder start with a bare one.

## What it does (vs. TokScript)

| TokScript feature | LocalScript |
|---|---|
| Transcript from TikTok / Reels / Shorts links | ✅ Also YouTube, Facebook, X and anything else yt-dlp supports |
| Bulk import (up to 50 links) | ✅ Paste many links, one per line; they queue and run one after another |
| Profile / playlist / collection import | ⚠️ YouTube playlists and channels work. TikTok profiles and collections are often blocked by TikTok unless you pick **Sign-in → your browser** (see below) |
| Export TXT, XML, JSON, CSV | ✅ Plus SRT and VTT subtitles. Optional timestamps. Bulk export as a `.zip` |
| History + bookmarks | ✅ Searchable, starred, stored in SQLite |
| Translate | ✅ Speech → English (switches to `large-v3` automatically, see Notes) |
| HD video + cover download | ✅ "Save HD video" (H.264/AAC mp4, no TikTok watermark where yt-dlp returns the clean stream). Cover is always saved |
| AI script / hook analyzer, MCP server | ❌ Not built. See "Ideas" |

## Optional: Parakeet Ultra engine

[Parakeet Ultra](https://huggingface.co/moondream/parakeet-ultra) (Moondream's post-trained NVIDIA Parakeet v3, 0.6B) is faster and lighter than Whisper but covers only **25 European languages** and **can't translate**. LocalScript's default model, **`auto`**, therefore routes per video: a quick Whisper-tiny language check on the first 30 s sends clear speech in a supported language to Parakeet, and everything else (other languages, doubtful or music-heavy clips, Translate) to Whisper.

One-time setup, which builds `transcribe.cpp` (MIT) from source with Metal into `~/.localscript/engines/`:

```bash
./scripts/install_parakeet.sh
```

The script pins a tested commit and works around incomplete Command Line Tools installs (missing C++ headers). The 740 MB model downloads on first use, or is reused if you already have `handy-computer/parakeet-ultra-gguf` in your Hugging Face cache (the Handy app uses the same file). Without the setup, `auto` simply uses Whisper.

Measured on an M1 Pro, one 7:49 English talk (transcription only, model already loaded):

| Engine | Time | Speed |
|---|---|---|
| Parakeet Ultra | 11 s | 41× realtime |
| Whisper large-v3-turbo | 21 s | 22× realtime |
| Whisper large-v3 | ~75 s end to end (incl. download) | ~6× |

For a 20 s Short, either engine takes a couple of seconds and the download dominates, so the gain shows on long videos and big batches. Transcripts differed by about 5% of words: mostly style (Parakeet writes "gonna" and keeps "um"; Whisper normalises), plus a few real misses on each side. Parakeet misheard "Pizza" as "Pixa" in one Short. Spot-check anything important.

## Command line

```bash
uv run localscript transcribe https://www.tiktok.com/@user/video/123            # prints text
uv run localscript transcribe URL1 URL2 --format srt,json,txt -o ~/transcripts   # writes files
uv run localscript transcribe -f links.txt --timestamps -o out/                   # links from a file
uv run localscript transcribe URL --translate                                     # English output
uv run localscript transcribe URL --video -o out/                                 # also keep the mp4
uv run localscript list -q pizza                                                  # search history
```

Options: `-m auto|parakeet-ultra|tiny|base|small|medium|large-v3-turbo|large-v3` (default `auto`), `-l es` (force the spoken language), `--limit 50`, `--no-expand`, `--cookies-from-browser chrome`.

The CLI and the web app share one history, so a CLI run shows up in the UI.

## Notes and limits

- **Models.** `auto` (default) picks Parakeet or Whisper per video (see above); with no Parakeet set up it uses `large-v3-turbo`. Among Whisper sizes, `large-v3-turbo` is close to the best accuracy; `tiny`/`base` are quicker but hallucinate more on noisy clips.
- **Translate.** Neither Parakeet nor `large-v3-turbo` can translate (turbo returns empty text), so translation switches to Whisper `large-v3` (~3 GB first-time download).
- **Music-only clips.** Whisper invents text over music (and its output on such clips can change with the audio encoding). Very low-confidence segments are dropped, so a clip with no speech can show *"No speech detected"*. Sung lyrics can come through as text. Parakeet doesn't report confidence, so nothing is filtered on its output.
- **Instagram and private videos** usually need a logged-in session. Choose your browser under **Sign-in** (CLI: `--cookies-from-browser chrome`). yt-dlp reads that browser's cookies locally; macOS may ask for Keychain access.
- **TikTok.** Single video links work out of the box (the `curl-cffi` dependency lets yt-dlp present a real-browser fingerprint). Profile and collection listings are blocked often; use Sign-in or paste individual links.
- **Platforms change.** If a site stops working, update the downloader: `uv lock --upgrade-package yt-dlp && uv sync`.
- **Security.** The server binds to `127.0.0.1` only, rejects non-localhost `Host` headers (DNS-rebinding guard) and only accepts JSON bodies (no cross-site form posts). Don't expose it with `--host 0.0.0.0` unless you trust your network.
- Please only download content you have the right to use, and respect each platform's terms.

## Where things live

- Code: `localscript/` (`core.py` pipeline and exports, `engine.py` one job, `server.py` API + queue, `cli.py`, `app.py` desktop window, `appbundle.py` builds the .app, `static/index.html` UI)
- Installed copy that the app runs: `~/.local/share/uv/tools/localscript/` (command: `~/.local/bin/localscript`)
- Data: `~/.localscript/` (`history.db`, `media/<job>/` with covers and any saved videos). Override with `LOCALSCRIPT_HOME`.
- Parakeet engine (optional): `~/.localscript/engines/transcribe.cpp/`, built by `scripts/install_parakeet.sh`.
- Models: the Hugging Face cache, `~/.cache/huggingface/hub/`.

## Ideas for next

- **MCP server** exposing `transcribe(url)` so Claude can pull transcripts directly (TokScript's "MCP" feature).
- **Script analyzer**: send a finished transcript to the Claude API for hook / structure / CTA breakdowns.
- **Word-level timestamps** (`word_timestamps=True`) for karaoke-style captions.
