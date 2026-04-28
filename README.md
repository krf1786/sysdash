# sysdash

sysdash is a local macOS system and developer dashboard. It is a FastAPI server with a single static browser UI, live WebSocket updates, and guarded action endpoints for common machine-maintenance tasks.

This README is written partly for future AI agents. Read it before changing the app.

## What It Does

sysdash shows a dense, macOS-styled command center for:

- CPU, RAM, swap, disk, network, battery, and thermal state.
- Top CPU/RAM processes, with guarded kill buttons.
- Listening ports, active TCP connections, and dev-server health checks.
- Docker containers, Homebrew services, dev runtimes, git repos, auth status, toolchain versions, logs, disk hogs, cleanup actions, installed packages, and outdated packages.
- A clickable cheat sheet that opens allowed commands in macOS Terminal.
- A Dev Launcher that scans browser-selected folders for projects, stores launchers in browser localStorage, checks launcher health, and opens saved project commands in macOS Terminal.
- Plain-English diagnostics with a `resolve` flow for safe automatic fixes.

The app is intended for Kyle's local Mac. It is not a hosted, multi-user, or hardened public web app.

## Quick Start

From this folder:

```sh
./run.sh
```

`run.sh`:

- Finds Python 3.12.
- Creates or rebuilds `.venv`.
- Installs `requirements.txt`.
- Runs `server.py`.
- Writes the active port to `~/.sysdash-port`.

Open the current server:

```sh
open "http://localhost:$(cat ~/.sysdash-port)"
```

The configured port is currently in `config.json`. If that port is occupied, `server.py` picks a free localhost port.

## User Launch Options

- `./run.sh` starts the server in Terminal.
- `Open Sysdash.command` is a double-click launcher for macOS Finder.
- `install-autostart.sh` installs a LaunchAgent so the server starts at login.
- `MenuBarApp/SysdashMenu.app` is a small native macOS menu-bar helper that opens sysdash and can start it.

Logs commonly used while debugging:

```sh
tail -n 80 sysdash.log
tail -n 80 sysdash.err
tail -n 80 sysdash-menu.log
tail -n 80 sysdash-menu.err
```

## Architecture

```text
sysdash/
├── server.py                 FastAPI app, WebSocket loop, action endpoints
├── collectors/
│   ├── system.py             CPU, RAM, disk, network, internet, battery, thermals
│   ├── dev.py                ports, Docker, brew, runtimes, git, toolchain, packages
│   └── extras.py             auth, logs, cleanup, diagnostics, cheatsheet, disk hogs
├── static/
│   ├── index.html            dashboard markup
│   ├── styles.css            full UI styling
│   ├── app.js                WebSocket client, renderers, UI actions
│   └── graphlogo.png         top-bar logo
├── MenuBarApp/
│   └── SysdashMenu.swift     macOS menu-bar helper source
├── config.json               local app config
├── requirements.txt          Python dependencies
├── run.sh                    server launcher
├── install-autostart.sh      LaunchAgent installer
└── README.md
```

The server is intentionally simple:

- `server.py` starts a background `broadcast_loop()`.
- Every 1.5 seconds it calls `gather_metrics()`.
- Fast collectors run every tick.
- Slow collectors are cached on configurable intervals.
- Latest metrics are sent to `/ws` subscribers and exposed at `/api/snapshot`.

The UI is intentionally dependency-free:

- No frontend build step.
- No React/Vite/npm app.
- `static/app.js` directly manipulates the DOM.
- UI state such as theme, card order, collapsed cards, pinned cards, and launchers lives in browser `localStorage`.

## Important Endpoints

Read-only:

- `GET /` returns `static/index.html`.
- `GET /api/snapshot` returns the latest metrics JSON.
- `GET /api/config` returns `config.json`.
- `GET /api/cleanup/actions` lists cleanup actions.
- `WS /ws` streams live metric snapshots.

Settings:

- `POST /api/config` validates and writes `config.json`.

Actions:

- `POST /api/kill/{pid}` sends SIGTERM only to user-owned processes.
- `POST /api/free-port/{port}` kills user-owned listeners on a port.
- `POST /api/docker/{start|stop|restart}/{cid}` controls Docker containers.
- `POST /api/diagnostic` recomputes diagnostic findings.
- `POST /api/resolve` runs safe issue resolvers.
- `POST /api/cleanup/preview` estimates cleanup targets.
- `POST /api/cleanup/run` performs a selected cleanup action.
- `POST /api/cheats/run` opens an allowed cheat-sheet command in Terminal.
- `POST /api/terminal/run` runs an allowed cheat-sheet command inline.
- `POST /api/launcher/run` opens a saved launcher command in Terminal.
- `POST /api/launcher/infer-base` guesses a browser-selected folder's real path.
- `POST /api/launcher/health` checks saved launcher health.
- `POST /api/launcher/fix` creates Python venvs and installs requirements for Python launchers.
- `POST /api/packages/update` updates one outdated brew/npm/pip package.
- `POST /api/packages/update-all` updates up to 30 selected outdated packages.

## Configuration

Edit `config.json` or use the settings panel in the dashboard.

Important keys:

- `port`: preferred localhost port.
- `watched_repos`: folders shown in the git panel.
- `log_files`: files scanned for recent errors.
- `alert_thresholds`: CPU/RAM/disk/memory-pressure alert thresholds.
- `dev_server_health_checks.default_path`: path used when checking local listening ports.
- `feature_flags`: toggles for optional panels.
- `*_interval_sec`: refresh cadence for slower collectors.

Port changes require restarting the server.

## Safety Rules For Future Agents

Do not loosen these without a clear user request:

- The server binds to `127.0.0.1`, not all interfaces.
- Process killing must stay limited to user-owned processes.
- Port freeing must stay limited to user-owned listeners.
- Cheat-sheet and inline terminal commands must stay allowlisted.
- Launcher commands must keep the dangerous-pattern blocklist.
- Launcher working directories must stay inside the user's home folder.
- Cleanup actions should be explicit, previewable where possible, and avoid arbitrary paths from the browser.
- Package update endpoints should only update packages already detected in inventory.

This app executes local commands. Treat every new action endpoint as privileged.

## UI Conventions

The current UI goal is a polished macOS system-dashboard feel:

- Dense, scannable cards.
- No horizontal scrolling.
- Top bar contains the logo, `sysdash`, health text, status pills, theme toggle, settings, and help.
- The top summary strip is `#command-center`.
- Cards have small type badges, accent colors, and pin/left/up/down/right/collapse controls.
- Do not add marketing-style hero sections or large explanatory blocks.
- Keep buttons and controls compact.
- Use existing CSS variables and existing card/action patterns before inventing new ones.
- If adding a card, include a stable `data-card-id` and a sensible `data-kind` accent.

Recent requested UI behavior:

- The top-left logo is `static/graphlogo.png`.
- The fake macOS window dots were intentionally removed from the dashboard header.
- The decorative five-strip top rail was intentionally removed.
- Cards can be moved left/right as well as up/down.

## Dev Launcher Notes

The browser cannot reveal absolute folder paths from folder selection. The launcher therefore stores:

- Detected project type and command.
- A user-provided or inferred base folder.
- A `cwd` used by `/api/launcher/run`.

When fixing launcher path bugs, check both browser-side path assembly in `static/app.js` and server-side validation in `server.py`.

Python launcher health/fix is intentionally narrow:

- It detects missing local venvs.
- It can create `.venv`.
- It installs `requirements.txt` if present.
- It does not guess arbitrary missing imports unless that package is listed.

## Validation Checklist

After backend changes:

```sh
python3 -m py_compile server.py collectors/*.py
```

After frontend changes:

```sh
node --check static/app.js
python3 -m html.parser static/index.html
awk 'BEGIN{oc=0;cc=0}{oc+=gsub(/\{/,"{");cc+=gsub(/\}/,"}")}END{print oc, cc}' static/styles.css
```

The CSS check should print matching numbers.

For visual changes, open:

```sh
open "http://localhost:$(cat ~/.sysdash-port)"
```

If using Playwright from Codex, prefer the bundled wrapper at:

```sh
~/.codex/skills/playwright/scripts/playwright_cli.sh
```

## Common Issues

`ModuleNotFoundError` in a launched project:

- The launcher is opening the correct folder, but that project needs its own venv or dependencies.
- Use the launcher's health/fix button for Python projects with `requirements.txt`.

The dashboard port changed:

- Read `~/.sysdash-port`.
- The preferred port in `config.json` may have been occupied.

No port health shown:

- Only local listening ports are checked.
- Some servers return 404/403 on `/`; set `dev_server_health_checks.default_path` or interpret the status code.

Auth status says logged out:

- The dashboard shells out to tools like `gh`, `aws`, `gcloud`, `docker`, and `npm`.
- If the CLI is missing or its session expired, sysdash reports that.

Package status is gray/unknown:

- Some managers do not expose update status cheaply or the command failed.
- The package list is cached by `package_inventory_interval_sec`.

## Dependency Notes

Python dependencies are minimal:

- `fastapi`
- `uvicorn`
- `psutil`

External command integrations are best-effort and optional:

- `docker`
- `brew`
- `git`
- `npm`
- `pip`
- `gh`
- `aws`
- `gcloud`
- macOS tools such as `osascript`, `memory_pressure`, `pmset`, `xcrun`, `lsof`, and `netstat`

Missing tools should degrade gracefully in collectors and render as unavailable, not crash the dashboard.
