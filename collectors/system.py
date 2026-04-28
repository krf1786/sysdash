"""Core system metrics: CPU, RAM, disk, network, thermals, battery, memory pressure."""
from __future__ import annotations
import os
import shutil
import subprocess
import time
from typing import Any

import psutil


# psutil's first cpu_percent call returns 0.0; prime it on import.
psutil.cpu_percent(interval=None, percpu=True)
_LAST_NET = psutil.net_io_counters()
_LAST_NET_TS = time.time()


def cpu_ram() -> dict[str, Any]:
    cpu_overall = psutil.cpu_percent(interval=None)
    cpu_per_core = psutil.cpu_percent(interval=None, percpu=True)
    load1, load5, load15 = os.getloadavg()
    cores = psutil.cpu_count(logical=True) or 1

    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()

    # Top processes by CPU and by RAM. We snapshot once and sort.
    procs = []
    for p in psutil.process_iter(["pid", "name", "username", "memory_info", "cpu_percent"]):
        try:
            info = p.info
            procs.append({
                "pid": info["pid"],
                "name": (info["name"] or "")[:40],
                "user": info.get("username") or "",
                "rss_mb": round((info["memory_info"].rss if info["memory_info"] else 0) / (1024 * 1024), 1),
                "cpu_pct": info.get("cpu_percent") or 0.0,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    top_ram = sorted(procs, key=lambda x: x["rss_mb"], reverse=True)[:10]
    top_cpu = sorted(procs, key=lambda x: x["cpu_pct"], reverse=True)[:10]

    return {
        "cpu": {
            "overall_pct": cpu_overall,
            "per_core": cpu_per_core,
            "core_count": cores,
            "load_avg": [load1, load5, load15],
            "load_explainer": _explain_load(load1, cores),
        },
        "ram": {
            "total_gb": round(vm.total / (1024**3), 2),
            "used_gb": round(vm.used / (1024**3), 2),
            "available_gb": round(vm.available / (1024**3), 2),
            "pct": vm.percent,
            "swap_used_gb": round(sm.used / (1024**3), 2),
            "swap_pct": sm.percent,
            "pressure": _memory_pressure(),
        },
        "top_ram": top_ram,
        "top_cpu": top_cpu,
    }


def _explain_load(load1: float, cores: int) -> str:
    ratio = load1 / cores if cores else 0
    if ratio < 0.7:
        return f"Idle. {load1:.2f} jobs queued on {cores} cores."
    if ratio < 1.0:
        return f"Busy but fine. {load1:.2f} jobs on {cores} cores."
    if ratio < 2.0:
        return f"Saturated. {load1:.2f} jobs on {cores} cores - things will feel slow."
    return f"Overloaded. {load1:.2f} jobs on {cores} cores - machine is thrashing."


def _memory_pressure() -> dict[str, Any]:
    """macOS memory pressure: green/yellow/red. Uses memory_pressure CLI."""
    try:
        out = subprocess.run(
            ["memory_pressure"],
            capture_output=True, text=True, timeout=2
        )
        text = (out.stdout or "") + (out.stderr or "")
        # Parse: "System-wide memory free percentage: 42%"
        free_pct = None
        for line in text.splitlines():
            if "free percentage" in line.lower():
                try:
                    free_pct = int(line.split(":")[-1].strip().rstrip("%"))
                except ValueError:
                    pass
        if free_pct is None:
            return {"level": "unknown", "free_pct": None, "explainer": "Could not read memory_pressure."}
        if free_pct >= 30:
            return {"level": "green", "free_pct": free_pct, "explainer": "Plenty of room. macOS is happy."}
        if free_pct >= 15:
            return {"level": "yellow", "free_pct": free_pct, "explainer": "Getting tight. macOS may start compressing."}
        return {"level": "red", "free_pct": free_pct, "explainer": "Critical. Apps may be killed soon."}
    except Exception as e:
        return {"level": "unknown", "free_pct": None, "explainer": f"err: {e}"}


def disk() -> dict[str, Any]:
    parts = []
    seen = set()
    for p in psutil.disk_partitions(all=False):
        if _skip_disk_partition(p):
            continue
        if p.device in seen:
            continue
        seen.add(p.device)
        try:
            u = psutil.disk_usage(p.mountpoint)
        except PermissionError:
            continue
        parts.append({
            "device": p.device,
            "mount": p.mountpoint,
            "total_gb": round(u.total / (1024**3), 1),
            "used_gb": round(u.used / (1024**3), 1),
            "free_gb": round(u.free / (1024**3), 1),
            "pct": u.percent,
        })

    io = psutil.disk_io_counters()
    return {
        "partitions": parts,
        "io": {
            "read_mb": round((io.read_bytes if io else 0) / (1024**2), 1),
            "write_mb": round((io.write_bytes if io else 0) / (1024**2), 1),
        },
    }


def _skip_disk_partition(part: psutil._common.sdiskpart) -> bool:
    """Hide non-actionable simulator runtime mounts from disk-full warnings."""
    mount = part.mountpoint
    opts = set((part.opts or "").split(","))

    if mount.startswith("/Library/Developer/CoreSimulator/"):
        return True

    return "ro" in opts and mount != "/"


def network() -> dict[str, Any]:
    global _LAST_NET, _LAST_NET_TS
    now = psutil.net_io_counters()
    ts = time.time()
    dt = max(ts - _LAST_NET_TS, 0.001)
    up_bps = (now.bytes_sent - _LAST_NET.bytes_sent) / dt
    down_bps = (now.bytes_recv - _LAST_NET.bytes_recv) / dt
    _LAST_NET = now
    _LAST_NET_TS = ts

    conns = 0
    try:
        conns = len(psutil.net_connections(kind="inet"))
    except (psutil.AccessDenied, RuntimeError):
        pass

    return {
        "up_kbps": round(up_bps / 1024, 1),
        "down_kbps": round(down_bps / 1024, 1),
        "total_sent_mb": round(now.bytes_sent / (1024**2), 1),
        "total_recv_mb": round(now.bytes_recv / (1024**2), 1),
        "active_connections": conns,
    }


def battery_thermals() -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        b = psutil.sensors_battery()
        if b:
            out["battery"] = {
                "pct": round(b.percent, 1),
                "plugged": b.power_plugged,
                "secs_left": b.secsleft if b.secsleft != psutil.POWER_TIME_UNLIMITED else None,
            }
    except Exception:
        pass

    # CPU temp via psutil (limited on macOS) - try a non-blocking pmset/powermetrics fallback skipped.
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for label, entries in temps.items():
                if entries:
                    out["temperature_c"] = round(entries[0].current, 1)
                    out["temperature_label"] = label
                    break
    except Exception:
        pass
    return out


def internet_check() -> dict[str, Any]:
    """Quick reachability ping. Done with shutil.which + subprocess to avoid blocking."""
    results = {}
    ping = shutil.which("ping") or "/sbin/ping"
    for name, host in [("Internet", "1.1.1.1"), ("DNS", "8.8.8.8"), ("GitHub", "github.com")]:
        ok = False
        try:
            r = subprocess.run(
                [ping, "-c", "1", "-W", "1000", host],
                capture_output=True, timeout=2
            )
            ok = r.returncode == 0
        except Exception:
            ok = False
        results[name] = ok
    return results
