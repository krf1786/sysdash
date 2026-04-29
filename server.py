"""sysdash server - FastAPI + WebSocket. Polls collectors, broadcasts to UI, exposes action endpoints."""
from __future__ import annotations
import asyncio
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psutil
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from collectors import system as csys
from collectors import dev as cdev
from collectors import extras as cextras


HERE = Path(__file__).parent
CONFIG_PATH = HERE / "config.json"
PORT_FILE = Path.home() / ".sysdash-port"


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")


def _clean_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(x).strip() for x in value if str(x).strip()]


def _validated_config(payload: dict[str, Any]) -> dict[str, Any]:
    current = load_config()
    cfg = dict(current)

    if "port" in payload:
        port = int(payload["port"])
        if not 1024 <= port <= 65535:
            raise HTTPException(status_code=400, detail="Port must be between 1024 and 65535")
        cfg["port"] = port

    cfg["watched_repos"] = _clean_str_list(payload.get("watched_repos", cfg.get("watched_repos", [])))
    cfg["log_files"] = _clean_str_list(payload.get("log_files", cfg.get("log_files", [])))

    thresholds = dict(cfg.get("alert_thresholds", {}))
    incoming_thresholds = payload.get("alert_thresholds", {})
    if isinstance(incoming_thresholds, dict):
        for key in ["cpu_pct", "ram_pct", "disk_free_pct"]:
            if key in incoming_thresholds:
                val = int(incoming_thresholds[key])
                if not 1 <= val <= 100:
                    raise HTTPException(status_code=400, detail=f"{key} must be 1-100")
                thresholds[key] = val
        if "memory_pressure_red" in incoming_thresholds:
            thresholds["memory_pressure_red"] = bool(incoming_thresholds["memory_pressure_red"])
    cfg["alert_thresholds"] = thresholds

    flags = dict(cfg.get("feature_flags", {}))
    incoming_flags = payload.get("feature_flags", {})
    if isinstance(incoming_flags, dict):
        for key in ["show_thermals", "show_battery", "show_outdated_packages", "show_auth_status", "show_disk_hogs"]:
            if key in incoming_flags:
                flags[key] = bool(incoming_flags[key])
    cfg["feature_flags"] = flags

    for key, minimum in [
        ("outdated_check_interval_sec", 60),
        ("package_inventory_interval_sec", 120),
        ("disk_hog_check_interval_sec", 60),
        ("data_hog_check_interval_sec", 300),
        ("auth_check_interval_sec", 30),
    ]:
        if key in payload:
            cfg[key] = max(minimum, int(payload[key]))

    return cfg


def pick_port() -> int:
    preferred = load_config().get("port")
    if isinstance(preferred, int) and 1024 <= preferred <= 65535:
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
        finally:
            s.close()

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def host_info() -> dict[str, str]:
    ip = ""
    try:
        for _, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == socket.AF_INET and not addr.address.startswith("127."):
                    ip = addr.address
                    raise StopIteration
    except StopIteration:
        pass
    except Exception:
        ip = ""

    mac_ver = platform.mac_ver()[0]
    if not mac_ver and shutil.which("sw_vers"):
        try:
            mac_ver = subprocess.run(
                ["sw_vers", "-productVersion"],
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.strip()
        except Exception:
            mac_ver = ""
    major = mac_ver.split(".", 1)[0] if mac_ver else ""
    mac_names = {
        "26": "Tahoe",
        "15": "Sequoia",
        "14": "Sonoma",
        "13": "Ventura",
        "12": "Monterey",
        "11": "Big Sur",
        "10": "macOS",
    }
    mac_name = mac_names.get(major, "")
    os_label = f"macOS {mac_name} {mac_ver}".strip() if mac_name else (f"macOS {mac_ver}" if mac_ver else platform.system())
    return {
        "hostname": socket.gethostname().split(".")[0],
        "os": os_label,
        "machine": platform.machine(),
        "ip": ip,
    }


# ---- background state ----
class State:
    def __init__(self) -> None:
        self.config = load_config()
        self.subscribers: set[WebSocket] = set()
        self.last_metrics: dict[str, Any] = {}
        self.cached_outdated: dict[str, Any] = {}
        self.cached_packages: list[dict[str, Any]] = []
        self.cached_disk_hogs: list[dict[str, Any]] = []
        self.cached_data_hogs: list[dict[str, Any]] = []
        self.cached_auth: list[dict[str, Any]] = []
        self.data_hogs_refreshing = False
        self.last_outdated_ts = 0.0
        self.last_packages_ts = 0.0
        self.last_disk_hogs_ts = 0.0
        self.last_data_hogs_ts = 0.0
        self.last_auth_ts = 0.0


STATE = State()


def sysdash_footprint() -> dict[str, Any]:
    """Resource use for this dashboard server and its child helpers."""
    proc = psutil.Process(os.getpid())
    processes = [proc]
    try:
        processes.extend(proc.children(recursive=True))
    except Exception:
        pass

    rss = 0
    cpu = 0.0
    threads = 0
    children = 0
    for item in processes:
        try:
            with item.oneshot():
                rss += item.memory_info().rss
                cpu += item.cpu_percent(interval=None)
                threads += item.num_threads()
                if item.pid != proc.pid:
                    children += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    uptime = max(0, int(time.time() - proc.create_time()))
    return {
        "pid": proc.pid,
        "rss_mb": round(rss / (1024 * 1024), 1),
        "cpu_pct": round(cpu, 1),
        "threads": threads,
        "children": children,
        "uptime_sec": uptime,
        "port": PORT_FILE.read_text().strip() if PORT_FILE.exists() else "",
    }


async def _refresh_package_inventory() -> None:
    STATE.cached_packages = await asyncio.to_thread(cdev.package_inventory)
    STATE.last_packages_ts = time.time()


async def _refresh_data_hogs() -> None:
    if STATE.data_hogs_refreshing:
        return
    STATE.data_hogs_refreshing = True
    try:
        STATE.cached_data_hogs = await asyncio.to_thread(cextras.data_volume_hogs)
        STATE.last_data_hogs_ts = time.time()
    finally:
        STATE.data_hogs_refreshing = False


async def _refresh_slow_metrics() -> None:
    """Run expensive checks on a longer cadence."""
    cfg = STATE.config
    now = time.time()

    auth_ivl = cfg.get("auth_check_interval_sec", 120)
    if now - STATE.last_auth_ts >= auth_ivl:
        STATE.cached_auth = await asyncio.to_thread(cextras.auth_status)
        STATE.last_auth_ts = now

    disk_ivl = cfg.get("disk_hog_check_interval_sec", 300)
    if now - STATE.last_disk_hogs_ts >= disk_ivl:
        STATE.cached_disk_hogs = await asyncio.to_thread(cextras.disk_hogs)
        STATE.last_disk_hogs_ts = now

    data_ivl = cfg.get("data_hog_check_interval_sec", 900)
    if now - STATE.last_data_hogs_ts >= data_ivl and not STATE.data_hogs_refreshing:
        STATE.last_data_hogs_ts = now
        asyncio.create_task(_refresh_data_hogs())

    out_ivl = cfg.get("outdated_check_interval_sec", 600)
    if now - STATE.last_outdated_ts >= out_ivl:
        STATE.cached_outdated = await asyncio.to_thread(cextras.outdated_packages)
        STATE.last_outdated_ts = now

    pkg_ivl = cfg.get("package_inventory_interval_sec", 900)
    if now - STATE.last_packages_ts >= pkg_ivl:
        STATE.last_packages_ts = now
        asyncio.create_task(_refresh_package_inventory())


async def gather_metrics() -> dict[str, Any]:
    cfg = STATE.config
    # Fast collectors run every tick.
    sys_block = await asyncio.to_thread(csys.cpu_ram)
    disk_block = await asyncio.to_thread(csys.disk)
    net_block = await asyncio.to_thread(csys.network)
    bt_block = await asyncio.to_thread(csys.battery_thermals)
    ports = await asyncio.to_thread(cdev.listening_ports)
    active_ports = await asyncio.to_thread(cdev.active_ports)
    docker = await asyncio.to_thread(cdev.docker_containers)
    services = await asyncio.to_thread(cdev.brew_services)
    runtimes = await asyncio.to_thread(cdev.detect_runtimes)
    llms = await asyncio.to_thread(cdev.local_llms)
    inet = await asyncio.to_thread(csys.internet_check)
    sshvpn = await asyncio.to_thread(cextras.ssh_vpn_sessions)

    # Health-check listening ports as URLs (best-effort; never break gather)
    port_health = []
    if ports:
        try:
            port_health = await cextras.dev_server_health(
                [p["port"] for p in ports if 1024 <= p["port"] <= 65535],
                cfg.get("dev_server_health_checks", {}).get("default_path", "/"),
            )
        except Exception as e:
            print(f"[sysdash] port-health check skipped: {e}")
            port_health = []

    # Slow collectors run periodically (cached)
    await _refresh_slow_metrics()

    # Less-frequent
    log_lines = await asyncio.to_thread(cextras.log_tail, cfg.get("log_files", []), 20)
    git = await asyncio.to_thread(cdev.git_status_for_repos, cfg.get("watched_repos", []))
    toolchain = await asyncio.to_thread(cdev.toolchain_versions)
    cheats = cextras.cheatsheet()

    metrics = {
        "ts": time.time(),
        "system": sys_block,
        "disk": disk_block,
        "net": net_block,
        "battery_thermals": bt_block,
        "ports": ports,
        "active_ports": active_ports,
        "port_health": port_health,
        "docker": docker,
        "services": services,
        "runtimes": runtimes,
        "llms": llms,
        "git": git,
        "toolchain": toolchain,
        "internet": inet,
        "ssh_vpn": sshvpn,
        "log_errors": log_lines,
        "auth": STATE.cached_auth,
        "disk_hogs": STATE.cached_disk_hogs,
        "data_hogs": STATE.cached_data_hogs,
        "outdated": STATE.cached_outdated,
        "packages": STATE.cached_packages,
        "cheatsheet": cheats,
        "host": host_info(),
        "self": sysdash_footprint(),
        "alerts": _compute_alerts({"system": sys_block, "ram": sys_block.get("ram", {}), "disk": disk_block}),
    }
    metrics["diagnostic"] = cextras.diagnostic({
        "system": sys_block,
        "disk": disk_block,
        "net": net_block,
        "internet": inet,
    })
    return metrics


def _compute_alerts(m: dict[str, Any]) -> list[dict[str, Any]]:
    alerts = []
    cfg = STATE.config.get("alert_thresholds", {})
    cpu_pct = m.get("system", {}).get("cpu", {}).get("overall_pct", 0)
    ram_pct = m.get("ram", {}).get("pct", 0)
    pressure = (m.get("ram", {}).get("pressure") or {}).get("level")

    if cpu_pct >= cfg.get("cpu_pct", 90):
        alerts.append({"level": "red", "key": "cpu", "msg": f"CPU at {cpu_pct:.0f}%"})
    if ram_pct >= cfg.get("ram_pct", 85):
        alerts.append({"level": "yellow", "key": "ram", "msg": f"RAM at {ram_pct:.0f}%"})
    if cfg.get("memory_pressure_red", True) and pressure == "red":
        alerts.append({"level": "red", "key": "mem_pressure", "msg": "Memory pressure RED"})
    for part in m.get("disk", {}).get("partitions", []):
        free_pct = 100 - part.get("pct", 0)
        if free_pct < cfg.get("disk_free_pct", 10):
            alerts.append({"level": "red", "key": f"disk:{part['mount']}", "msg": f"{part['mount']} {free_pct:.0f}% free"})
    return alerts


async def broadcast_loop() -> None:
    while True:
        try:
            STATE.last_metrics = await gather_metrics()
            payload = json.dumps(STATE.last_metrics, default=str)
            dead = []
            for ws in list(STATE.subscribers):
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                STATE.subscribers.discard(ws)
        except Exception as e:
            print(f"[sysdash] broadcast error: {e}")
        await asyncio.sleep(1.5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(broadcast_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


def _post_json_url(url: str, payload: dict[str, Any], timeout: float = 60.0) -> tuple[int, Any, float]:
    started = time.time()
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read(1_000_000).decode("utf-8", errors="replace")
        data = json.loads(raw) if raw else {}
        return response.status, data, max(time.time() - started, 0.001)


def _local_llm_chat(payload: dict[str, Any]) -> dict[str, Any]:
    message = str(payload.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Missing message")
    if len(message) > 4000:
        raise HTTPException(status_code=400, detail="Message is too long")

    requested_port = int(payload["port"]) if payload.get("port") else None
    requested_model = str(payload.get("model") or "").strip()
    history = payload.get("history") if isinstance(payload.get("history"), list) else []
    clean_history = []
    for item in history[-10:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = str(item.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            clean_history.append({"role": role, "content": content[:4000]})

    llms = cdev.local_llms()
    servers = [s for s in llms.get("servers", []) if s.get("status") == "api"]
    if requested_port:
        servers = [s for s in servers if int(s.get("port") or 0) == requested_port]
    if not servers:
        raise HTTPException(status_code=503, detail="No local LLM API is online. Start LM Studio or Ollama, then refresh sysdash.")

    server = servers[0]
    spec = next((s for s in cdev.LLM_SERVERS if int(s["port"]) == int(server["port"])), None)
    if not spec:
        raise HTTPException(status_code=503, detail="That local LLM server is not supported yet")

    model = requested_model or (server.get("models") or [""])[0]
    messages = [
        {
            "role": "system",
            "content": "You are a concise local coding assistant inside sysdash. Help with shell, Python, JavaScript, local debugging, and explain risks before destructive actions.",
        },
        *clean_history,
        {"role": "user", "content": message},
    ]

    try:
        if spec["kind"] == "ollama":
            if not model:
                raise HTTPException(status_code=400, detail="Ollama has no loaded model listed")
            _status, data, elapsed = _post_json_url(
                f"http://127.0.0.1:{server['port']}/api/chat",
                {
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "keep_alive": "10m",
                    "options": {"temperature": 0.35, "num_predict": 512},
                },
                timeout=90,
            )
            reply = ((data.get("message") or {}).get("content") or "").strip()
            tokens = data.get("eval_count")
        elif spec["kind"] == "openai":
            if not model:
                raise HTTPException(status_code=400, detail="No model is available from that server")
            _status, data, elapsed = _post_json_url(
                f"http://127.0.0.1:{server['port']}/v1/chat/completions",
                {
                    "model": model,
                    "messages": messages,
                    "max_tokens": 512,
                    "temperature": 0.35,
                    "stream": False,
                },
                timeout=90,
            )
            choices = data.get("choices") or []
            reply = (((choices[0] or {}).get("message") or {}).get("content") or "").strip() if choices else ""
            tokens = (data.get("usage") or {}).get("completion_tokens")
        else:
            raise HTTPException(status_code=400, detail=f"{server['name']} chat is not supported yet")
    except HTTPException:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise HTTPException(status_code=503, detail=f"Local LLM request failed: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not reply:
        raise HTTPException(status_code=502, detail="The local LLM returned an empty response")

    return {
        "ok": True,
        "reply": reply,
        "server": server["name"],
        "port": server["port"],
        "model": model,
        "elapsed_sec": round(elapsed, 2),
        "tokens": tokens,
        "tokens_per_sec": round(float(tokens) / elapsed, 2) if tokens else None,
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((HERE / "static" / "index.html").read_text())


@app.get("/api/snapshot")
async def snapshot() -> JSONResponse:
    if not STATE.last_metrics:
        STATE.last_metrics = await gather_metrics()
    return JSONResponse(STATE.last_metrics)


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    return load_config()


@app.post("/api/config")
async def update_config(payload: dict[str, Any]) -> dict[str, Any]:
    cfg = _validated_config(payload)
    save_config(cfg)
    STATE.config = cfg
    STATE.last_auth_ts = 0
    STATE.last_disk_hogs_ts = 0
    STATE.last_data_hogs_ts = 0
    STATE.last_outdated_ts = 0
    STATE.last_packages_ts = 0
    return {"ok": True, "config": cfg, "detail": "Settings saved. Restart sysdash to apply a port change."}


@app.post("/api/data-hogs/refresh")
async def refresh_data_hogs() -> dict[str, Any]:
    if STATE.data_hogs_refreshing:
        for _ in range(70):
            await asyncio.sleep(0.5)
            if not STATE.data_hogs_refreshing:
                break
    await _refresh_data_hogs()
    return {"ok": True, "data_hogs": STATE.cached_data_hogs}


@app.post("/api/data-hogs/delete")
async def delete_data_hog(payload: dict[str, Any]) -> dict[str, Any]:
    raw_path = str(payload.get("path") or "").strip()
    if not raw_path:
        raise HTTPException(status_code=400, detail="Missing file path")

    data_root = Path("/System/Volumes/Data").resolve()
    path = Path(raw_path).expanduser().resolve(strict=False)
    if data_root not in path.parents:
        raise HTTPException(status_code=400, detail="Only /System/Volumes/Data files can be moved from this panel")
    if not path.exists():
        STATE.last_data_hogs_ts = 0
        return {"ok": False, "detail": "That file is already gone.", "data_hogs": STATE.cached_data_hogs}
    if not path.is_file():
        raise HTTPException(status_code=400, detail="Only individual files can be moved to Trash from this panel")

    trash = Path.home() / ".Trash"
    trash.mkdir(exist_ok=True)
    dest = trash / path.name
    if dest.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = trash / f"{path.stem}-{stamp}{path.suffix}"

    try:
        before_mb = round(path.stat().st_size / (1024 ** 2), 1)
        shutil.move(str(path), str(dest))
    except PermissionError:
        raise HTTPException(status_code=403, detail="macOS did not allow sysdash to move that file")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    STATE.last_data_hogs_ts = 0
    await _refresh_data_hogs()
    return {
        "ok": True,
        "detail": f"Moved {path.name} ({before_mb} MB) to Trash.",
        "trash_path": str(dest),
        "data_hogs": STATE.cached_data_hogs,
    }


@app.post("/api/llm/chat")
async def llm_chat(payload: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(_local_llm_chat, payload)


@app.websocket("/ws")
async def ws(ws: WebSocket) -> None:
    await ws.accept()
    STATE.subscribers.add(ws)
    try:
        if STATE.last_metrics:
            await ws.send_text(json.dumps(STATE.last_metrics, default=str))
        while True:
            # Just keep connection alive; we ignore inbound messages.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        STATE.subscribers.discard(ws)


# ---- actions ----
def _is_user_owned(pid: int) -> bool:
    try:
        return psutil.Process(pid).username() == os.getlogin()
    except Exception:
        return False


@app.post("/api/kill/{pid}")
async def kill_pid(pid: int) -> dict[str, Any]:
    if not _is_user_owned(pid):
        raise HTTPException(status_code=403, detail="Will not kill non-user-owned processes from sysdash")
    try:
        os.kill(pid, signal.SIGTERM)
        return {"ok": True, "pid": pid, "signal": "SIGTERM"}
    except ProcessLookupError:
        raise HTTPException(status_code=404, detail="No such PID")
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")


@app.post("/api/free-port/{port}")
async def free_port(port: int) -> dict[str, Any]:
    killed = []
    try:
        for c in psutil.net_connections(kind="tcp"):
            if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN and c.pid:
                if _is_user_owned(c.pid):
                    try:
                        os.kill(c.pid, signal.SIGTERM)
                        killed.append(c.pid)
                    except Exception:
                        pass
    except (psutil.AccessDenied, RuntimeError):
        pass
    return {"ok": bool(killed), "port": port, "killed_pids": killed}


@app.post("/api/docker/{action}/{cid}")
async def docker_action(action: str, cid: str) -> dict[str, Any]:
    if action not in ("start", "stop", "restart"):
        raise HTTPException(status_code=400, detail="Invalid action")
    try:
        r = subprocess.run(["docker", action, cid], capture_output=True, text=True, timeout=10)
        return {"ok": r.returncode == 0, "stdout": r.stdout.strip(), "stderr": r.stderr.strip()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/process/{pid}")
async def process_detail(pid: int) -> dict[str, Any]:
    try:
        p = psutil.Process(pid)
        with p.oneshot():
            mem = p.memory_info()
            parent = p.parent()
            cmdline = p.cmdline()
            detail = {
                "ok": True,
                "pid": pid,
                "name": p.name(),
                "user": p.username(),
                "status": p.status(),
                "cpu_pct": p.cpu_percent(interval=0.05),
                "rss_mb": round(mem.rss / (1024 * 1024), 1),
                "threads": p.num_threads(),
                "created": p.create_time(),
                "cmd": " ".join(cmdline) if cmdline else "",
                "exe": "",
                "cwd": "",
                "parent": {"pid": parent.pid, "name": parent.name()} if parent else None,
            }
            try:
                detail["exe"] = p.exe()
            except Exception:
                pass
            try:
                detail["cwd"] = p.cwd()
            except Exception:
                pass
        ports = []
        try:
            for c in p.net_connections(kind="inet"):
                ports.append({
                    "local": f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else "",
                    "remote": f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else "",
                    "status": c.status,
                })
        except Exception:
            ports = []
        detail["ports"] = ports[:12]
        detail["guess"] = _process_guess(detail)
        return detail
    except psutil.NoSuchProcess:
        return {"ok": False, "detail": "Process is no longer running."}
    except psutil.AccessDenied:
        return {"ok": False, "detail": "macOS did not allow sysdash to inspect that process."}


def _process_guess(detail: dict[str, Any]) -> str:
    haystack = f"{detail.get('name', '')} {detail.get('cmd', '')} {detail.get('exe', '')}".lower()
    if any(x in haystack for x in ("chrome", "safari", "firefox")):
        return "Browser process. High memory can be normal with many tabs or devtools open."
    if "node" in haystack or "npm" in haystack:
        return "Node/dev-server process. Check the project folder and listening ports before killing it."
    if "python" in haystack or "uvicorn" in haystack:
        return "Python process. It may be a local app, script, notebook, or sysdash helper."
    if "docker" in haystack:
        return "Docker helper. Killing it may stop containers or Docker Desktop services."
    if "code" in haystack or "electron" in haystack:
        return "Editor or Electron helper. Many helper processes are normal."
    return "Regular user process. Inspect the command and parent before killing it."


@app.post("/api/diagnostic")
async def run_diagnostic() -> dict[str, Any]:
    if not STATE.last_metrics:
        STATE.last_metrics = await gather_metrics()
    return {"findings": cextras.diagnostic(STATE.last_metrics)}


def _free_gb(path: str) -> float:
    usage = shutil.disk_usage(path)
    return round(usage.free / (1024**3), 1)


def _remove_children(path: Path) -> tuple[int, list[str]]:
    removed = 0
    errors = []
    if not path.exists():
        return removed, errors
    for child in path.iterdir():
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
            removed += 1
        except Exception as e:
            errors.append(f"{child.name}: {e}")
    return removed, errors


def _resolve_disk_pressure() -> dict[str, Any]:
    before = _free_gb("/System/Volumes/Data")
    removed = 0
    errors = []

    for path in [Path.home() / "Library" / "Caches", Path.home() / ".cache"]:
        count, path_errors = _remove_children(path)
        removed += count
        errors.extend(path_errors[:4])

    xcrun = shutil.which("xcrun")
    if xcrun:
        subprocess.run(
            [xcrun, "simctl", "runtime", "dyld_shared_cache", "remove", "--all"],
            capture_output=True,
            text=True,
            timeout=30,
        )

    after = _free_gb("/System/Volumes/Data")
    STATE.last_disk_hogs_ts = 0
    return {
        "ok": after > before,
        "title": "Disk cleanup attempted",
        "detail": f"Freed about {max(0, after - before):.1f} GB. Free space is now {after:.1f} GB.",
        "removed_items": removed,
        "errors": errors[:8],
    }


def _resolve_network_check() -> dict[str, Any]:
    status = csys.internet_check()
    ok = all(status.values())
    return {
        "ok": ok,
        "title": "Network check refreshed",
        "detail": "Connectivity is back." if ok else "Ping checks are still failing. Check VPN, DNS, firewall, or Wi-Fi.",
        "status": status,
    }


CLEANUP_TARGETS: dict[str, dict[str, Any]] = {
    "pip-cache": {"label": "pip cache", "paths": [Path.home() / "Library" / "Caches" / "pip"]},
    "npm-cache": {"label": "npm cache", "paths": [Path.home() / ".npm"]},
    "brew-cache": {"label": "Homebrew cache", "cmd": ["brew", "cleanup", "-s"]},
    "xcode-derived": {"label": "Xcode DerivedData", "paths": [Path.home() / "Library" / "Developer" / "Xcode" / "DerivedData"]},
    "xcode-archives": {"label": "Xcode Archives", "paths": [Path.home() / "Library" / "Developer" / "Xcode" / "Archives"]},
    "sim-unavailable": {"label": "Unavailable Simulators", "cmd": ["xcrun", "simctl", "delete", "unavailable"]},
    "user-caches": {"label": "User caches", "paths": [Path.home() / "Library" / "Caches", Path.home() / ".cache"]},
    "playwright-cache": {"label": "Playwright browsers", "paths": [Path.home() / "Library" / "Caches" / "ms-playwright"]},
    "bun-cache": {"label": "Bun cache", "paths": [Path.home() / ".bun" / "install" / "cache"]},
    "pnpm-store": {"label": "pnpm store", "cmd": ["pnpm", "store", "prune"]},
    "yarn-cache": {"label": "Yarn cache", "cmd": ["yarn", "cache", "clean"]},
    "uv-cache": {"label": "uv cache", "cmd": ["uv", "cache", "clean"]},
    "go-build-cache": {"label": "Go build cache", "cmd": ["go", "clean", "-cache", "-testcache"]},
    "go-mod-cache": {"label": "Go module cache", "cmd": ["go", "clean", "-modcache"]},
    "docker-prune": {"label": "Docker prune", "cmd": ["docker", "system", "prune", "-f"]},
}


def _cleanup_action(action: str) -> dict[str, Any]:
    spec = CLEANUP_TARGETS.get(action)
    if not spec:
        return {"ok": False, "title": "Unknown cleanup action", "detail": action, "errors": []}

    before = _free_gb("/System/Volumes/Data")
    removed = 0
    errors: list[str] = []
    stdout = ""
    stderr = ""

    if "paths" in spec:
        for path in spec["paths"]:
            count, path_errors = _remove_children(path)
            removed += count
            errors.extend(path_errors[:6])

    if "cmd" in spec:
        cmd = spec["cmd"]
        exe = shutil.which(cmd[0])
        if not exe:
            errors.append(f"{cmd[0]} is not installed")
        else:
            run_cmd = [exe, *cmd[1:]]
            try:
                r = subprocess.run(
                    run_cmd,
                    cwd=str(Path.home()),
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                stdout = r.stdout[-3000:]
                stderr = r.stderr[-2000:]
                if r.returncode != 0:
                    errors.append(f"{cmd[0]} exited {r.returncode}")
            except subprocess.TimeoutExpired:
                errors.append(f"{cmd[0]} cleanup timed out")

    after = _free_gb("/System/Volumes/Data")
    STATE.last_disk_hogs_ts = 0
    return {
        "ok": not errors,
        "title": f"{spec['label']} cleanup finished",
        "detail": f"Freed about {max(0, after - before):.2f} GB. Free space is now {after:.2f} GB.",
        "removed_items": removed,
        "stdout": stdout,
        "stderr": stderr,
        "errors": errors[:8],
    }


def _cleanup_preview(action: str) -> dict[str, Any]:
    spec = CLEANUP_TARGETS.get(action)
    if not spec:
        return {"ok": False, "title": "Unknown cleanup action", "detail": action, "targets": []}
    targets = []
    total = 0.0
    for path in spec.get("paths", []):
        gb = cextras._du(str(path))  # local helper returns None when missing/unreadable
        targets.append({"path": str(path), "gb": gb or 0.0, "exists": path.exists()})
        total += gb or 0.0
    cmd = spec.get("cmd")
    if cmd:
        targets.append({"path": " ".join(cmd), "gb": 0.0, "exists": bool(shutil.which(cmd[0])), "command": True})
    return {
        "ok": True,
        "id": action,
        "label": spec["label"],
        "estimated_gb": round(total, 2),
        "targets": targets,
    }


def _recommended_cleanup_actions(min_gb: float = 0.01) -> list[dict[str, Any]]:
    picks = []
    for action in CLEANUP_TARGETS:
        preview = _cleanup_preview(action)
        if preview.get("estimated_gb", 0) >= min_gb:
            picks.append(preview)
    return sorted(picks, key=lambda x: x.get("estimated_gb", 0), reverse=True)


@app.get("/api/cleanup/actions")
async def cleanup_actions() -> dict[str, Any]:
    return {
        "actions": [
            {"id": key, "label": spec["label"]}
            for key, spec in CLEANUP_TARGETS.items()
        ]
    }


@app.get("/api/cleanup/recommended")
async def cleanup_recommended() -> dict[str, Any]:
    picks = await asyncio.to_thread(_recommended_cleanup_actions)
    return {
        "ok": True,
        "actions": picks,
        "estimated_gb": round(sum(p.get("estimated_gb", 0) for p in picks), 2),
    }


@app.post("/api/cleanup/preview")
async def cleanup_preview(payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action", "")).strip()
    if not action:
        raise HTTPException(status_code=400, detail="Missing cleanup action")
    return await asyncio.to_thread(_cleanup_preview, action)


@app.post("/api/cleanup/run")
async def cleanup_run(payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action", "")).strip()
    if not action:
        raise HTTPException(status_code=400, detail="Missing cleanup action")
    result = await asyncio.to_thread(_cleanup_action, action)
    STATE.last_metrics = await gather_metrics()
    result["disk_hogs"] = STATE.last_metrics.get("disk_hogs", [])
    return result


@app.post("/api/cleanup/recommended/run")
async def cleanup_recommended_run() -> dict[str, Any]:
    picks = await asyncio.to_thread(_recommended_cleanup_actions)
    results = []
    for item in picks[:12]:
        results.append(await asyncio.to_thread(_cleanup_action, str(item["id"])))
    STATE.last_metrics = await gather_metrics()
    return {
        "ok": all(r.get("ok") for r in results) if results else True,
        "count": len(results),
        "title": "Recommended cleanup finished",
        "detail": "No cleanup targets had measurable reclaimable space." if not results else f"Ran {len(results)} cleanup action{'' if len(results) == 1 else 's'}.",
        "results": results,
        "disk_hogs": STATE.last_metrics.get("disk_hogs", []),
    }


@app.post("/api/resolve")
async def resolve_issue(payload: dict[str, Any]) -> dict[str, Any]:
    finding = str(payload.get("finding", ""))
    if not finding:
        raise HTTPException(status_code=400, detail="Missing finding")

    if "full" in finding and ("/System/Volumes/Data" in finding or "disk" in finding.lower()):
        result = await asyncio.to_thread(_resolve_disk_pressure)
    elif finding.startswith("Network:"):
        result = await asyncio.to_thread(_resolve_network_check)
    else:
        result = {
            "ok": False,
            "title": "No automatic resolver yet",
            "detail": "sysdash knows about this issue, but there is not a safe one-click fix for it yet.",
        }

    STATE.last_metrics = await gather_metrics()
    result["diagnostic"] = STATE.last_metrics.get("diagnostic", [])
    result["alerts"] = STATE.last_metrics.get("alerts", [])
    return result


def _applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


@app.post("/api/cheats/run")
async def run_cheat(payload: dict[str, Any]) -> dict[str, Any]:
    cmd = str(payload.get("cmd", "")).strip()
    allowed = {row["cmd"] for row in cextras.cheatsheet()}
    if not cmd:
        raise HTTPException(status_code=400, detail="Missing command")
    if cmd not in allowed:
        raise HTTPException(status_code=403, detail="Command is not in the sysdash cheat sheet")

    osascript = shutil.which("osascript") or "/usr/bin/osascript"
    script = (
        'tell application "Terminal"\n'
        "  activate\n"
        f"  do script {_applescript_string(cmd)}\n"
        "end tell"
    )
    try:
        r = subprocess.run([osascript, "-e", script], capture_output=True, text=True, timeout=8)
        return {
            "ok": r.returncode == 0,
            "cmd": cmd,
            "stdout": r.stdout.strip(),
            "stderr": r.stderr.strip(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/terminal/run")
async def terminal_run(payload: dict[str, Any]) -> dict[str, Any]:
    cmd = str(payload.get("cmd", "")).strip()
    allowed = {row["cmd"] for row in cextras.cheatsheet()}
    if not cmd:
        raise HTTPException(status_code=400, detail="Missing command")
    if cmd not in allowed:
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": "This terminal box only runs sysdash cheat-sheet commands. Add the command to the cheat sheet first, or use macOS Terminal.",
        }

    shell = os.environ.get("SHELL") or "/bin/zsh"
    env = os.environ.copy()
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    try:
        r = subprocess.run(
            [shell, "-lc", cmd],
            cwd=str(Path.home()),
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return {
            "ok": r.returncode == 0,
            "returncode": r.returncode,
            "stdout": r.stdout[-8000:],
            "stderr": r.stderr[-4000:],
        }
    except subprocess.TimeoutExpired as e:
        return {
            "ok": False,
            "returncode": None,
            "stdout": (e.stdout or "")[-8000:] if isinstance(e.stdout, str) else "",
            "stderr": "Command timed out after 20 seconds.",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


SHORTCUT_EXTS = {
    ".app", ".command", ".workflow", ".scpt", ".sh", ".py", ".js", ".html",
    ".webloc", ".url", ".terminal",
}


def _shortcut_path(value: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Missing shortcut path")
    if raw == "/projects" or raw.startswith("/projects/"):
        root = Path("/projects")
        if not root.exists():
            raw = str(Path.home() / "projects" / raw.removeprefix("/projects").lstrip("/"))
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.home() / path
    try:
        return path.resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="Could not resolve shortcut path")


def _shortcut_roots() -> list[Path]:
    roots = [Path.home().resolve(), Path("/Applications")]
    projects = Path("/projects")
    if projects.exists():
        roots.append(projects.resolve())
    return roots


def _shortcut_allowed(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, f"Shortcut does not exist: {path}"
    ext = ".app" if path.suffix == ".app" or str(path).endswith(".app") else path.suffix.lower()
    if ext not in SHORTCUT_EXTS:
        return False, f"Unsupported shortcut type: {ext or 'folder'}"
    for root in _shortcut_roots():
        try:
            path.relative_to(root)
            return True, ""
        except ValueError:
            continue
    return False, "Shortcut must live in your home folder, /projects, or /Applications"


@app.post("/api/shortcuts/scan")
async def shortcuts_scan(payload: dict[str, Any]) -> dict[str, Any]:
    folder = _shortcut_path(str(payload.get("dir", "~/projects")))
    if not folder.exists() or not folder.is_dir():
        return {"ok": False, "detail": f"Folder not found: {folder}", "shortcuts": []}
    for root in _shortcut_roots():
        try:
            folder.relative_to(root)
            break
        except ValueError:
            pass
    else:
        return {"ok": False, "detail": "Folder must be inside your home folder, /projects, or /Applications", "shortcuts": []}

    found = []
    base_depth = len(folder.parts)
    try:
        for path in folder.rglob("*"):
            if len(path.parts) - base_depth > 4:
                continue
            if path.is_dir() and path.suffix != ".app":
                continue
            ok, _reason = _shortcut_allowed(path)
            if not ok:
                continue
            ext = ".app" if str(path).endswith(".app") else path.suffix.lower()
            if path.name == "__init__.py":
                continue
            if ext in {".sh", ".py", ".js"} and path.parent != folder and not os.access(path, os.X_OK):
                continue
            found.append({
                "name": path.stem if path.suffix else path.name,
                "path": str(path),
                "ext": ext,
            })
            if len(found) >= 100:
                break
    except PermissionError:
        pass
    found.sort(key=lambda x: (x["ext"], x["name"].lower()))
    return {"ok": True, "dir": str(folder), "shortcuts": found}


@app.post("/api/shortcuts/run")
async def shortcuts_run(payload: dict[str, Any]) -> dict[str, Any]:
    path = _shortcut_path(str(payload.get("path", "")))
    ok, reason = _shortcut_allowed(path)
    if not ok:
        return {"ok": False, "stderr": reason, "path": str(path)}
    opener = shutil.which("open") or "/usr/bin/open"
    try:
        r = subprocess.run([opener, str(path)], capture_output=True, text=True, timeout=8)
        return {
            "ok": r.returncode == 0,
            "path": str(path),
            "stdout": r.stdout.strip(),
            "stderr": r.stderr.strip(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _launcher_command_allowed(cmd: str) -> tuple[bool, str]:
    lowered = cmd.strip().lower()
    blocked = [
        "rm ",
        "sudo ",
        "shutdown",
        "reboot",
        "halt",
        "diskutil erase",
        "diskutil partition",
        "mkfs",
        "dd ",
        "launchctl bootout",
        ":(){",
    ]
    for token in blocked:
        if lowered.startswith(token) or f"; {token}" in lowered or f"&& {token}" in lowered or f"| {token}" in lowered:
            return False, f"Blocked command pattern: {token.strip()}"
    return True, ""


def _launcher_cwd_allowed(cwd: str) -> tuple[Path | None, str]:
    if not cwd:
        return None, ""
    path = Path(cwd).expanduser()
    try:
        resolved = path.resolve()
    except Exception:
        return None, "Could not resolve project folder"
    home = Path.home().resolve()
    try:
        resolved.relative_to(home)
    except ValueError:
        return None, "Project folder must be inside your home folder"
    if not resolved.exists():
        return None, f"Project folder does not exist: {resolved}"
    if not resolved.is_dir():
        return None, f"Project folder is not a directory: {resolved}"
    return resolved, ""


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


@app.post("/api/launcher/run")
async def launcher_run(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name", "Project")).strip()[:80]
    cmd = str(payload.get("cmd", "")).strip()
    cwd = str(payload.get("cwd", "")).strip()
    if not cmd:
        raise HTTPException(status_code=400, detail="Missing command")
    if len(cmd) > 500:
        raise HTTPException(status_code=400, detail="Command is too long")

    ok, reason = _launcher_command_allowed(cmd)
    if not ok:
        return {"ok": False, "name": name, "cmd": cmd, "stderr": reason}

    cwd_path, cwd_reason = _launcher_cwd_allowed(cwd)
    if cwd_reason:
        return {"ok": False, "name": name, "cmd": cmd, "cwd": cwd, "stderr": cwd_reason}

    terminal_cmd = cmd
    if cwd_path:
        terminal_cmd = f"cd {_shell_quote(str(cwd_path))} && {cmd}"

    osascript = shutil.which("osascript") or "/usr/bin/osascript"
    script = (
        'tell application "Terminal"\n'
        "  activate\n"
        f"  do script {_applescript_string(terminal_cmd)}\n"
        "end tell"
    )
    try:
        r = subprocess.run([osascript, "-e", script], capture_output=True, text=True, timeout=8)
        return {
            "ok": r.returncode == 0,
            "name": name,
            "cmd": cmd,
            "cwd": str(cwd_path) if cwd_path else "",
            "stdout": r.stdout.strip(),
            "stderr": r.stderr.strip(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/launcher/infer-base")
async def launcher_infer_base(payload: dict[str, Any]) -> dict[str, Any]:
    root = Path(str(payload.get("root", "")).strip()).name
    if not root:
        return {"ok": False, "base": "", "reason": "Missing scan root"}
    home = Path.home()
    candidates = [
        home / root,
        home / "Desktop" / root,
        home / "Documents" / root,
        home / "Downloads" / root,
        home / "Projects" / root,
        home / "projects" / root,
    ]
    for candidate in candidates:
        if candidate.exists() and candidate.is_dir():
            return {"ok": True, "base": str(candidate)}
    return {"ok": False, "base": "", "reason": f"Could not find {root} in common folders"}


def _launcher_health(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name", "Project")).strip()[:80]
    cmd = str(payload.get("cmd", "")).strip()
    cwd = str(payload.get("cwd", "")).strip()
    path, reason = _launcher_cwd_allowed(cwd)
    if reason:
        return {
            "ok": False,
            "name": name,
            "status": "missing",
            "detail": reason,
            "fixable": False,
        }

    assert path is not None
    issues = []
    checks = []
    fixable = False
    is_python = bool(re.match(r"^python3?\s+\S+\.py", cmd))
    if is_python:
        has_venv = (path / ".venv" / "bin" / "python").exists() or (path / "venv" / "bin" / "python").exists()
        has_requirements = (path / "requirements.txt").exists()
        checks.append({"label": "Python project", "ok": True})
        checks.append({"label": "requirements.txt", "ok": has_requirements})
        checks.append({"label": "virtualenv", "ok": has_venv, "fixable": not has_venv})
        if not has_venv:
            issues.append("No local virtualenv")
            fixable = True
    script_match = re.match(r"^python3?\s+([^\s;&|]+\.py)", cmd)
    if script_match and not (path / script_match.group(1)).exists():
        issues.append(f"Missing {script_match.group(1)}")
        fixable = False
        checks.append({"label": script_match.group(1), "ok": False})

    if "npm" in cmd or "node" in cmd:
        pkg = path / "package.json"
        node_modules = path / "node_modules"
        needs_node_modules = True
        if pkg.exists():
            try:
                pkg_data = json.loads(pkg.read_text())
                needs_node_modules = any(
                    pkg_data.get(key)
                    for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")
                )
            except Exception:
                needs_node_modules = True
        checks.append({"label": "package.json", "ok": pkg.exists()})
        checks.append({
            "label": "node_modules",
            "ok": node_modules.exists() or not needs_node_modules,
            "fixable": pkg.exists() and needs_node_modules and not node_modules.exists(),
        })
        if not pkg.exists():
            issues.append("Missing package.json")
        elif needs_node_modules and not node_modules.exists():
            issues.append("Run npm install")
            fixable = True

    if ".env" not in cmd and (path / ".env.example").exists() and not (path / ".env").exists():
        checks.append({"label": ".env", "ok": False})
        issues.append("Missing .env")

    if cmd.startswith("bash ") or cmd.startswith("./"):
        script_name = cmd.split()[1] if cmd.startswith("bash ") and len(cmd.split()) > 1 else cmd.split()[0].removeprefix("./")
        checks.append({"label": script_name, "ok": (path / script_name).exists()})
        if not (path / script_name).exists():
            issues.append(f"Missing {script_name}")

    running = False
    try:
        needle = str(path)
        for proc in psutil.process_iter(["cmdline"]):
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if needle in cmdline:
                running = True
                break
    except Exception:
        running = False

    if issues:
        status = "warn" if fixable else "bad"
        detail = "; ".join(issues)
    else:
        status = "running" if running else "ready"
        detail = "Running" if running else "Ready"
    return {
        "ok": status in {"ready", "running"},
        "name": name,
        "status": status,
        "detail": detail,
        "checks": checks[:8],
        "running": running,
        "fixable": fixable,
    }


def _launcher_fix(payload: dict[str, Any]) -> dict[str, Any]:
    cmd = str(payload.get("cmd", "")).strip()
    cwd = str(payload.get("cwd", "")).strip()
    path, reason = _launcher_cwd_allowed(cwd)
    if reason:
        return {"ok": False, "title": "Launcher fix failed", "detail": reason}
    assert path is not None
    if "npm" in cmd or "node" in cmd:
        pkg = path / "package.json"
        if not pkg.exists():
            return {"ok": False, "title": "Launcher fix failed", "detail": "package.json was not found."}
        npm = shutil.which("npm")
        if not npm:
            return {"ok": False, "title": "Launcher fix failed", "detail": "npm was not found."}
        try:
            r = subprocess.run([npm, "install"], cwd=str(path), capture_output=True, text=True, timeout=180)
            if r.returncode != 0:
                return {"ok": False, "title": "npm install failed", "detail": r.stderr[-2000:] or r.stdout[-2000:]}
        except subprocess.TimeoutExpired:
            return {"ok": False, "title": "Launcher fix timed out", "detail": "npm install took too long."}
        return {"ok": True, "title": "Launcher fixed", "detail": f"Node dependencies are ready in {path}."}

    if not re.match(r"^python3?\s+\S+\.py", cmd):
        return {"ok": False, "title": "Launcher fix skipped", "detail": "This launcher type does not have an automatic fix yet."}

    python = sys.executable or shutil.which("python3")
    if not python:
        return {"ok": False, "title": "Launcher fix failed", "detail": "python3 was not found."}
    venv_dir = path / ".venv"
    venv_python = venv_dir / "bin" / "python"
    try:
        venv_warning = ""
        if not venv_python.exists():
            r = subprocess.run([python, "-m", "venv", "--copies", str(venv_dir)], cwd=str(path), capture_output=True, text=True, timeout=90)
            if r.returncode != 0 and not venv_python.exists():
                return {"ok": False, "title": "Virtualenv creation failed", "detail": r.stderr[-2000:] or r.stdout[-2000:]}
            if r.returncode != 0:
                venv_warning = r.stderr[-1200:] or r.stdout[-1200:]
        req = path / "requirements.txt"
        if req.exists() and req.read_text().strip():
            r = subprocess.run([str(venv_python), "-m", "pip", "install", "-r", str(req)], cwd=str(path), capture_output=True, text=True, timeout=180)
            if r.returncode != 0:
                return {"ok": False, "title": "Dependency install failed", "detail": r.stderr[-2000:] or r.stdout[-2000:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "title": "Launcher fix timed out", "detail": "Dependency setup took too long."}

    detail = f"Python environment is ready in {path}."
    if venv_warning:
        detail += " venv reported a warning, but the environment is usable."
    return {"ok": True, "title": "Launcher fixed", "detail": detail}


@app.post("/api/launcher/health")
async def launcher_health(payload: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(_launcher_health, payload)


@app.post("/api/launcher/fix")
async def launcher_fix(payload: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(_launcher_fix, payload)


def _package_update_cmd(manager: str, name: str) -> list[str] | None:
    if manager == "brew":
        brew = shutil.which("brew")
        return [brew, "upgrade", name] if brew else None
    if manager == "npm":
        npm = shutil.which("npm")
        return [npm, "update", "-g", name] if npm else None
    if manager == "pip":
        pip = shutil.which("pip3") or shutil.which("pip")
        return [pip, "install", "--upgrade", name] if pip else None
    return None


@app.post("/api/packages/update")
async def update_package(payload: dict[str, Any]) -> dict[str, Any]:
    manager = str(payload.get("manager", "")).strip()
    name = str(payload.get("name", "")).strip()
    if not manager or not name:
        raise HTTPException(status_code=400, detail="Missing manager or name")

    inventory = STATE.cached_packages or await asyncio.to_thread(cdev.package_inventory)
    match = next((p for p in inventory if p.get("manager") == manager and p.get("name") == name), None)
    if not match:
        raise HTTPException(status_code=404, detail="Package is not in inventory")
    if match.get("status") != "outdated":
        return {"ok": True, "cmd": "", "stdout": f"{name} is already current.", "stderr": ""}

    cmd = _package_update_cmd(manager, name)
    if not cmd or not cmd[0]:
        raise HTTPException(status_code=400, detail=f"No updater available for {manager}")

    env = os.environ.copy()
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    try:
        r = subprocess.run(
            cmd,
            cwd=str(Path.home()),
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired as e:
        return {
            "ok": False,
            "cmd": " ".join(cmd),
            "stdout": (e.stdout or "")[-8000:] if isinstance(e.stdout, str) else "",
            "stderr": "Update timed out after 180 seconds.",
        }

    STATE.last_packages_ts = 0
    STATE.last_outdated_ts = 0
    STATE.cached_outdated = await asyncio.to_thread(cextras.outdated_packages)
    STATE.last_outdated_ts = time.time()
    await _refresh_package_inventory()
    return {
        "ok": r.returncode == 0,
        "cmd": " ".join(cmd),
        "stdout": r.stdout[-8000:],
        "stderr": r.stderr[-4000:],
        "returncode": r.returncode,
    }


@app.post("/api/packages/update-all")
async def update_all_packages(payload: dict[str, Any]) -> dict[str, Any]:
    managers = payload.get("managers") or ["brew", "npm", "pip"]
    names = payload.get("names") or []
    if not isinstance(managers, list) or not all(isinstance(x, str) for x in managers):
        raise HTTPException(status_code=400, detail="Invalid managers")
    if names and (not isinstance(names, list) or not all(isinstance(x, str) for x in names)):
        raise HTTPException(status_code=400, detail="Invalid package list")

    allowed_managers = {m for m in managers if m in {"brew", "npm", "pip"}}
    inventory = STATE.cached_packages or await asyncio.to_thread(cdev.package_inventory)
    selected = [
        p for p in inventory
        if p.get("status") == "outdated"
        and p.get("manager") in allowed_managers
        and (not names or p.get("name") in names)
    ][:30]

    results = []
    for pkg in selected:
        cmd = _package_update_cmd(str(pkg["manager"]), str(pkg["name"]))
        if not cmd or not cmd[0]:
            results.append({"manager": pkg["manager"], "name": pkg["name"], "ok": False, "stderr": "No updater available"})
            continue
        env = os.environ.copy()
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        try:
            r = subprocess.run(
                cmd,
                cwd=str(Path.home()),
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
            )
            results.append({
                "manager": pkg["manager"],
                "name": pkg["name"],
                "ok": r.returncode == 0,
                "cmd": " ".join(cmd),
                "stdout": r.stdout[-1200:],
                "stderr": r.stderr[-1200:],
            })
        except subprocess.TimeoutExpired:
            results.append({"manager": pkg["manager"], "name": pkg["name"], "ok": False, "stderr": "Update timed out"})

    STATE.last_packages_ts = 0
    STATE.last_outdated_ts = 0
    await _refresh_package_inventory()
    STATE.cached_outdated = await asyncio.to_thread(cextras.outdated_packages)
    STATE.last_outdated_ts = time.time()
    return {
        "ok": all(r.get("ok") for r in results) if results else True,
        "count": len(results),
        "results": results,
    }


def main() -> None:
    import uvicorn

    port = pick_port()
    PORT_FILE.write_text(str(port))
    print(f"[sysdash] listening on http://localhost:{port}")
    print(f"[sysdash] open it with:  open http://localhost:{port}")
    print(f"[sysdash] port written to {PORT_FILE}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
