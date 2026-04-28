# CLAUDE.md — sysdash

## What this project is

**sysdash** is a local-only, real-time system monitoring dashboard for macOS. It runs a FastAPI server that polls system stats via `psutil` and streams them over a WebSocket to a single-page dark-themed HTML dashboard. It's designed for a developer on Apple Silicon who wants one browser tab showing everything about their machine.

It is **not** a multi-user app, not deployed to any server, and has no authentication. It binds to `127.0.0.1` only.

## Owner / environment

- **User:** Kyle Fleming, macOS on Apple Silicon (MacBook Air)
- **Multiple Pythons installed:** `python3.11`, `python3.12`, `python3.14` via Homebrew, `python3.13` via `/usr/local/bin`
- **Target runtime:** Python 3.12 (`/opt/homebrew/bin/python3.12`). Python 3.14 has broken `ensurepip` and missing wheels — avoid it.
- **Known macOS issue:** Homebrew Python 3.12 crashes on `import xml.parsers.expat` (pyexpat) unless `DYLD_LIBRARY_PATH` includes `/opt/homebrew/opt/expat/lib`. The user has this in `~/.zshrc` but launchd and non-login shells need it set explicitly. `run.sh` and `install-autostart.sh` both handle this.

## File structure

```
sysdash/
├── server.py              # FastAPI app: lifespan, WebSocket broadcast loop, REST API, actions
├── collectors/
│   ├── __init__.py        # empty
│   ├── system.py          # CPU, RAM, disk, network, battery/thermals, memory pressure, internet ping
│   ├── dev.py             # listening ports, Docker containers, brew services, runtimes, toolchain versions, git status
│   └── extras.py          # auth status (gh/aws/gcloud/docker/npm), log tail, disk hogs, outdated pkgs, SSH/VPN, dev-server health, diagnostic, cheatsheet
├── static/
│   ├── index.html         # 3-column grid layout, all section IDs, modal for confirm dialogs
│   ├── styles.css         # Dark theme, CSS custom properties, responsive grid breakpoints
│   └── app.js             # WebSocket client, all render functions, action buttons (kill/free-port/docker)
├── config.json            # User-editable: watched_repos, log_files, alert thresholds, feature flags, check intervals
├── requirements.txt       # fastapi, uvicorn, psutil, httpx, websockets
├── run.sh                 # Hardened launcher: finds python3.12, creates/rebuilds venv, installs deps, runs server
├── install-autostart.sh   # Creates launchd plist at ~/Library/LaunchAgents/com.sysdash.agent.plist
└── README.md              # User-facing readme
```

## Architecture

### Server (`server.py`)

- **Startup:** Picks a random free port, writes it to `~/.sysdash-port`, starts uvicorn.
- **Lifespan:** Creates a single `broadcast_loop` asyncio task.
- **Broadcast loop (1.5s interval):** Calls `gather_metrics()` which runs all collectors via `asyncio.to_thread()`, then sends the JSON blob to every connected WebSocket.
- **Slow metrics:** Auth status (120s), disk hogs (300s), outdated packages (600s) — cached in `State` and refreshed on cadence.
- **Static files:** Mounted at `/static`, index served at `/`.
- **Endpoints:**
  - `GET /` — serves `index.html`
  - `GET /api/snapshot` — one-shot JSON of all metrics
  - `WS /ws` — live metric stream (server pushes, client just keeps connection alive)
  - `POST /api/kill/{pid}` — SIGTERM a user-owned process
  - `POST /api/free-port/{port}` — kill listeners on a TCP port
  - `POST /api/docker/{action}/{cid}` — start/stop/restart a Docker container
  - `POST /api/diagnostic` — run plain-English diagnostic

### Collectors

Each collector function returns a dict/list. They're called from `gather_metrics()` in `server.py`.

**`collectors/system.py`** — uses `psutil` directly:
- `cpu_ram()` — overall %, per-core %, load avg with plain-English explainer, top 10 procs by CPU and RAM, memory pressure via macOS `memory_pressure` CLI, swap
- `disk()` — partitions with usage, I/O counters
- `network()` — throughput (delta-based KB/s), total sent/recv, active connections
- `battery_thermals()` — battery % and plug status, CPU temp if available
- `internet_check()` — pings 1.1.1.1, 8.8.8.8, github.com

**`collectors/dev.py`** — developer-oriented:
- `listening_ports()` — TCP LISTEN sockets with owning process name
- `docker_containers()` — `docker ps -a` parsed from JSON format
- `brew_services()` — `brew services list` parsed
- `detect_runtimes()` — running node/python/ruby/go/java/deno/bun processes with RSS
- `toolchain_versions()` — version + path for node, npm, python, pip, ruby, go, java, git, docker
- `git_status_for_repos(paths)` — branch, dirty, ahead/behind for repos under `config.json`'s `watched_repos`

**`collectors/extras.py`** — slower/heavier checks:
- `auth_status()` — login state for gh, aws, gcloud, Docker Hub, npm
- `log_tail(paths, n)` — last N error/fatal/critical lines from configured log files
- `disk_hogs()` — sizes of node_modules, caches, Xcode DerivedData, Docker disk, etc.
- `outdated_packages()` — counts from `brew outdated`, `npm outdated -g`, `pip3 list --outdated`
- `ssh_vpn_sessions()` — active outbound SSH connections + VPN interfaces (utun/ipsec/tun/tap)
- `dev_server_health(ports)` — async HTTP GET to each listening port, returns status code + ok bool (uses `httpx.AsyncClient` with `trust_env=False` to bypass proxies)
- `diagnostic(metrics)` — plain-English findings: high CPU, memory pressure, swap, disk full, internet down
- `cheatsheet()` — static list of useful terminal commands (click-to-copy in UI)

### Frontend (`static/`)

**`index.html`** — 3-column responsive grid (`2fr 2fr 1.2fr`, collapses at 1100px and 760px):
- Column 1: CPU (with per-core mini bars), RAM (with pressure), top processes by RAM and CPU
- Column 2: Diagnostic, listening ports, Docker, brew services, disk hogs, disk I/O, network
- Column 3: Internet, auth status, toolchain, dev runtimes, git repos, outdated packages, SSH/VPN, log errors, battery/thermals, cheatsheet
- Sticky header with status pills (internet, memory pressure) and "help me" diagnostic button
- Alert bar at top (red/yellow) when thresholds exceeded
- Confirm modal for destructive actions (kill process, free port, docker stop)

**`app.js`** — single-file vanilla JS, no build step:
- WebSocket connection with auto-reconnect (1.5s delay)
- `render(m)` dispatches to ~20 render functions, one per section
- Helper functions: `bar()` for progress bars, `row()` for label/value pairs, `pctClass()` for color thresholds
- Process tables have "kill" buttons, port table has "free" buttons, Docker has start/stop
- Browser notifications for new alerts (requests permission on first click)
- Cheatsheet rows copy command to clipboard on click

**`styles.css`** — dark terminal aesthetic:
- CSS custom properties for all colors (`--bg: #0b0d0e`, `--accent: #5cf0a8`, etc.)
- Monospace font stack (ui-monospace, SFMono, JetBrains Mono, Menlo)
- Color-coded dots: `.ok` (green), `.warn` (yellow), `.bad` (red), `.dim` (gray)
- Responsive: 3 columns > 1100px, 2 columns > 760px, 1 column on mobile

### Configuration (`config.json`)

```json
{
  "watched_repos": ["~/code", "~/projects", "~/dev"],
  "log_files": ["~/Library/Logs/system.log"],
  "alert_thresholds": {
    "cpu_pct": 90,
    "ram_pct": 85,
    "disk_free_pct": 10,
    "memory_pressure_red": true
  },
  "dev_server_health_checks": {
    "default_path": "/"
  },
  "feature_flags": {
    "show_thermals": true,
    "show_battery": true,
    "show_outdated_packages": true,
    "show_auth_status": true,
    "show_disk_hogs": true
  },
  "outdated_check_interval_sec": 600,
  "disk_hog_check_interval_sec": 300,
  "auth_check_interval_sec": 120
}
```

Note: `feature_flags` are defined but **not yet wired up** on the backend — all sections always render regardless of flag values.

## How to run

```bash
cd ~/sysdash
./run.sh
# opens http://localhost:<port> — port written to ~/.sysdash-port
```

`run.sh` handles everything: finds python3.12, creates/rebuilds venv if needed (with `--without-pip` + `get-pip.py` fallback for broken ensurepip), installs deps, verifies imports, launches server.

To autostart at login:
```bash
./install-autostart.sh
```

This creates a launchd agent at `~/Library/LaunchAgents/com.sysdash.agent.plist` with `KeepAlive: true`. Logs go to `sysdash.log` and `sysdash.err` in the project directory.

## Dependencies

```
fastapi>=0.110      # Web framework + WebSocket support
uvicorn>=0.27       # ASGI server
psutil>=5.9         # System metrics (CPU, RAM, disk, network, processes, battery)
httpx>=0.27         # Async HTTP client for dev-server health checks
websockets>=12.0    # WebSocket protocol support for uvicorn (required — without it WS fails silently)
```

No frontend build tools. No database. No external services. Everything runs locally via subprocess calls and `psutil`.

## Key gotchas for future work

1. **`websockets` is required.** Uvicorn won't serve WebSocket connections without it — the dashboard shows "disconnected, retrying..." with no server-side error. It's in `requirements.txt` now but was missing originally.

2. **DYLD_LIBRARY_PATH for expat.** Python 3.12 on this machine crashes on `import xml.parsers.expat` without `DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib`. Both `run.sh` and `install-autostart.sh` set this. If pip or venv creation fails with `_XML_SetAllocTrackerActivationThreshold` symbol not found, this is why.

3. **Port is random.** The server picks a free port at startup. The port is written to `~/.sysdash-port`. The frontend connects to `ws://${location.host}/ws` so it always matches.

4. **No git repo.** This project is not in a git repository.

5. **`python3` on this machine resolves to 3.14.** Never use bare `python3` — always use `/opt/homebrew/bin/python3.12` explicitly or the venv's `python`.

6. **Collector errors are swallowed.** Most collectors catch all exceptions and return empty results. If a section shows "no data" or "checking...", the collector may be failing silently. Check the terminal output for `[sysdash]` prefixed warnings.

7. **`config.json` is read once at startup.** Changing it requires restarting the server.

8. **All subprocess calls have timeouts** (2-15s). If a tool hangs (e.g., `docker` when daemon is stopped), it won't block the broadcast loop forever, but that section's data will be stale.
