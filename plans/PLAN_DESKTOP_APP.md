# Plan: Baxter as a downloadable desktop app

**Status: potential future work — not started, not scheduled.** Captured from a
design discussion (2026-07-15, at commit `13f231b`) so the context isn't lost.

## Goal

Package Baxter as an application a tournament director can download and run on
their own machine, with no internet access required. The platonic ideal: a
single executable that starts a local server process and opens a browser
pointed at it. Double-click → browser opens → run the tournament.

## Why this is feasible

Baxter is already well-shaped for this:

- SQLite is the default database (`baxter/settings.py` falls back to
  `sqlite:///db.sqlite3` when `DATABASE_URL` is unset).
- Static files are served by whitenoise with
  `CompressedManifestStaticFilesStorage` — no separate web server needed.
- All config is env-driven (django-environ), so a desktop launcher can supply
  everything programmatically.
- The Rust pairing extension (`scrabble_pairing_py`) is just a compiled
  extension module (`.so`/`.pyd`) — packagers bundle it like any other binary
  dependency.

The obstacles are packaging Python itself and a handful of "server-ware"
assumptions in settings (secure cookies, proxy SSL header, registration-less
auth).

## Recommended stack

**PyInstaller + waitress + a small launcher script.** This is a well-trodden
path for Django desktop apps.

### 1. `desktop_launcher.py` entry point

A small script that becomes the executable's entry point. It:

- Sets `DJANGO_SETTINGS_MODULE` to a `baxter.settings_desktop` overlay.
- Puts the SQLite DB in a per-user data dir via `platformdirs`
  (`~/.local/share/baxter/` on Linux, `%APPDATA%\baxter\` on Windows,
  `~/Library/Application Support/baxter/` on macOS) — **not** next to the
  executable, which may be in a read-only location and gets replaced on
  upgrade.
- On first run, generates a random `SECRET_KEY` and stores it in that data
  dir. This preserves the existing fail-fast policy (settings.py deliberately
  has no SECRET_KEY default); the launcher supplies the key rather than the
  settings module growing a fallback.
- Runs migrations programmatically
  (`django.core.management.call_command("migrate")`) on every launch — this is
  both first-run DB creation and the upgrade path when the user downloads a
  new version.
- Binds **waitress** (pure-Python WSGI server, Windows-friendly — gunicorn
  does not run on Windows) to `127.0.0.1` on a free port. Pick the port with
  `socket.bind(("127.0.0.1", 0))`, or try a fixed favorite first and fall back.
- Calls `webbrowser.open(f"http://127.0.0.1:{port}")` once the server is
  accepting connections.

### 2. PyInstaller freeze

- The PyO3 extension is a non-issue: PyInstaller bundles compiled extension
  modules automatically.
- Django needs the usual coaxing in the `.spec` file: hidden imports
  (app configs, context processors referenced by string), and data files
  (templates, each app's `migrations/`, the collected `staticfiles/` tree
  including the whitenoise manifest).
- This part is mostly mechanical; the design decisions live in the launcher
  and settings overlay.

### 3. Statics baked at build time

Run `collectstatic` during the **build**, and bundle the `staticfiles/` output
as data. This matters especially because `django-node-assets` pulls assets
from `node_modules` — the desktop bundle must **not** ship Node or
`node_modules`. The end user's machine only ever sees the collected output,
served by whitenoise. (`qrcode` / `python-docx` are runtime output generators,
no packaging concern.)

### 4. `settings_desktop.py` overlay

Import from the base settings, then:

- `DEBUG = False`
- `ALLOWED_HOSTS = ["127.0.0.1", "localhost"]`
- **Drop** `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, and
  `SECURE_PROXY_SSL_HEADER` — the desktop app is plain HTTP on loopback, and
  secure cookies would silently break login. (In base settings these are
  applied whenever `not DEBUG`, so the overlay must actively undo them.)
- `DATABASES` pointed at the platformdirs data dir (launcher passes the path).
- `PAIRING_ENGINE` pinned to whatever has been burned in by then (`rust` once
  the cutover completes — one engine means one codepath to test in the frozen
  bundle).
- Static storage stays whitenoise manifest; `STATIC_ROOT` points into the
  bundled data.

Dependencies to exclude from the bundle: `psycopg2-binary`, `gunicorn`,
`django-debug-toolbar` (dev group already separate).

## Caveats to design around

### One build per platform

PyInstaller does not cross-compile. Build Windows/macOS/Linux artifacts via a
GitHub Actions matrix. Since the Rust wheel is already built with maturin, the
CI matrix covers both the extension and the freeze in one pass per OS.

### `--onefile` vs `--onedir`

True single-file (`--onefile`) exists but:

- it self-extracts to a temp dir on every launch (~2–5s startup penalty), and
- it is the #1 trigger for Windows antivirus false positives.

The pragmatic standard is `--onedir` shipped as a zip — still "download,
unzip, double-click `baxter.exe`". Start with onedir; offer onefile only if
users demand it. **Code-signing is the other real-world tax**: unsigned macOS
apps are quarantined by Gatekeeper, and unsigned Windows exes trip SmartScreen.
Budget for an Apple Developer ID and (optionally) a Windows signing cert
before wide distribution.

### Auth in a desktop context

The users app and login flow still exist. For a single-operator desktop app,
the first experience must not be a login wall with no way to register.
Options (decision deferred):

- First-run auto-creation of a local admin user (launcher prompts or generates
  credentials), or
- auto-login middleware for loopback connections (simplest UX; acceptable
  because anyone with local access owns the machine anyway).

### Process lifecycle

A plain launcher leaves a console window (fine on Linux, ugly on Windows —
build with `console=False` there) and offers no obvious "quit". Cheap options:

- a system-tray icon (`pystray`) with a Quit item, or
- shut down when a browser-tab heartbeat stops.

Don't over-invest here initially; a tray icon is probably enough.

## Alternatives considered

- **pywebview** instead of `webbrowser.open()`: wraps the OS webview in a
  native window — feels like a real app, one extra dependency, same
  architecture underneath. Nice optional upgrade later. Tauri/Electron would
  be strictly more machinery for the same result.
- **Nuitka** in place of PyInstaller: compiles rather than freezes; slightly
  better antivirus reputation and startup, but slower builds and more fiddly
  with Django's dynamic imports. Only reach for it if PyInstaller causes AV
  grief.
- **Briefcase (BeeWare)**: produces proper installers (.msi/.dmg) and handles
  signing scaffolding — worth a look if distribution polish becomes a goal,
  but more opinionated about project layout.
- **PyApp / shiv / zipapp**: either require Python preinstalled on the target
  machine or fetch a distribution at first run (needs internet) — both violate
  the requirements.
- **Docker**: rules itself out for "someone downloads and runs it"; the target
  user would need Docker installed.

## Phases

### Phase 1 — desktop settings + launcher (runs from a checkout)

`baxter/settings_desktop.py`, `desktop_launcher.py`, `platformdirs` +
`waitress` as deps. Verify: `uv run python desktop_launcher.py` on a machine
with no `.env` creates the data dir, generates a key, migrates, serves, and
opens a browser; login works (cookies not marked secure).

### Phase 2 — first-run auth story

Pick and implement one of the auth options above. Verify: fresh data dir →
usable tournament-editing session without touching a registration flow.

### Phase 3 — PyInstaller spec, single platform

`.spec` with Django hiddenimports + data files; `collectstatic` wired into the
build; onedir output. Verify on the dev (Linux) box: run the frozen bundle
from a directory with no repo checkout, no Python, no node_modules; create a
tournament end-to-end; confirm the Rust engine loads (`PAIRING_ENGINE=rust`).

### Phase 4 — CI matrix + distribution

GitHub Actions matrix (linux/windows/macos) producing zipped onedir bundles as
release artifacts. Windows: `console=False`. Verify: download artifact on a
clean VM per OS and repeat the Phase 3 smoke test.

### Phase 5 (optional polish) — lifecycle + packaging niceties

Tray icon / quit story, pywebview window, code-signing, installers
(Briefcase or plain .msi/.dmg) — each only if demand materializes.
