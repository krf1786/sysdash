"""Newbie-helper collectors: auth status, log tail, disk hogs, outdated pkgs, diagnostic, SSH/VPN, dev-server health."""
from __future__ import annotations
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
import psutil


def _run(cmd: list[str], timeout: float = 4.0) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return -1, "", str(e)


def auth_status() -> list[dict[str, Any]]:
    """One-row-per-tool login status. Green if logged in, red if not, gray if tool missing."""
    rows = []

    # GitHub CLI
    if shutil.which("gh"):
        rc, _, _ = _run(["gh", "auth", "status"], timeout=3)
        rows.append({"name": "GitHub (gh)", "ok": rc == 0, "installed": True})
    else:
        rows.append({"name": "GitHub (gh)", "ok": False, "installed": False})

    # AWS
    if shutil.which("aws"):
        rc, out, _ = _run(["aws", "sts", "get-caller-identity"], timeout=4)
        rows.append({"name": "AWS", "ok": rc == 0, "installed": True})
    else:
        rows.append({"name": "AWS", "ok": False, "installed": False})

    # gcloud
    if shutil.which("gcloud"):
        rc, out, _ = _run(["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"], timeout=4)
        rows.append({"name": "gcloud", "ok": rc == 0 and bool(out.strip()), "installed": True})
    else:
        rows.append({"name": "gcloud", "ok": False, "installed": False})

    # Docker Hub (logged in if ~/.docker/config.json contains an auth)
    cfg = Path.home() / ".docker" / "config.json"
    if shutil.which("docker"):
        ok = False
        if cfg.exists():
            try:
                ok = '"auths"' in cfg.read_text() and '"auth"' in cfg.read_text()
            except Exception:
                ok = False
        rows.append({"name": "Docker Hub", "ok": ok, "installed": True})
    else:
        rows.append({"name": "Docker Hub", "ok": False, "installed": False})

    # npm
    if shutil.which("npm"):
        rc, out, _ = _run(["npm", "whoami"], timeout=3)
        rows.append({"name": "npm", "ok": rc == 0 and bool(out.strip()), "installed": True})
    else:
        rows.append({"name": "npm", "ok": False, "installed": False})

    return rows


def log_tail(paths: list[str], lines: int = 20) -> list[dict[str, Any]]:
    """Tail recent ERROR/FATAL lines across configured log files."""
    out = []
    pattern = re.compile(r"\b(error|fatal|critical|panic|fail(?:ed)?)\b", re.IGNORECASE)
    for raw in paths:
        p = Path(os.path.expanduser(raw))
        if not p.exists() or not p.is_file():
            continue
        try:
            # Read last 200 lines, filter for error-y ones, take last N
            with p.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 64 * 1024))
                tail = f.read().decode("utf-8", errors="replace").splitlines()[-200:]
            errs = [ln for ln in tail if pattern.search(ln)][-lines:]
            for ln in errs:
                out.append({"file": str(p), "line": ln[:300]})
        except Exception:
            continue
    return out[-lines:]


def disk_hogs() -> dict[str, Any]:
    """Sizes of common dev-machine bloat directories."""
    targets = {
        "node_modules (across ~)": _glob_size("~/", "node_modules", max_depth=4),
        "~/Library/Caches": _du("~/Library/Caches"),
        "~/.cache": _du("~/.cache"),
        "Docker disk image": _docker_disk(),
        "Homebrew cache": _du("~/Library/Caches/Homebrew"),
        "pip cache": _du("~/Library/Caches/pip"),
        "npm cache": _du("~/.npm"),
        "Xcode DerivedData": _du("~/Library/Developer/Xcode/DerivedData"),
    }
    return [{"label": k, "gb": v} for k, v in targets.items() if v is not None]


def data_volume_hogs(limit: int = 5) -> list[dict[str, Any]]:
    """Top largest files on /System/Volumes/Data, best-effort with a hard timeout."""
    root = Path("/System/Volumes/Data")
    if not root.exists():
        return []

    user_name = Path.home().name
    user_root = root / "Users" / user_name
    candidates = [
        user_root / "Library" / "Containers" / "com.docker.docker",
        user_root / ".lmstudio",
        user_root / "Library" / "Application Support" / "Claude",
        user_root / "Library" / "Application Support" / "anythingllm-desktop",
        user_root / "Desktop",
        user_root,
        root / "private" / "var",
        root / "Library",
        root / "Applications",
        root / "opt",
        root / "usr" / "local",
    ]
    targets = [str(p) for p in candidates if p.exists()]
    if not targets:
        targets = [str(root)]

    rows_by_path: dict[str, dict[str, Any]] = {}
    for i, target in enumerate(targets):
        quoted = shlex.quote(target)
        cmd = (
            f"find {quoted} -xdev -type f -size +50M -print0 2>/dev/null | "
            "xargs -0 stat -f '%z\t%N' 2>/dev/null | "
            f"sort -nr | head -{int(limit)}"
        )
        # Known heavyweight app folders are quick. The broader home pass gets
        # more room, while system cache areas get a shorter best-effort pass.
        timeout = 45 if target == str(user_root) else 10
        rc, out, _ = _run(["/bin/zsh", "-lc", cmd], timeout=timeout)
        if rc != 0 and not out.strip():
            continue

        for line in out.splitlines():
            if "\t" not in line:
                continue
            raw_size, raw_path = line.split("\t", 1)
            try:
                size = int(raw_size)
            except ValueError:
                continue
            path = Path(raw_path)
            rows_by_path[raw_path] = {
                "name": path.name,
                "path": raw_path,
                "dir": str(path.parent),
                "gb": round(size / (1024 ** 3), 2),
                "mb": round(size / (1024 ** 2), 1),
                "_bytes": size,
            }

    rows = sorted(rows_by_path.values(), key=lambda r: r.get("_bytes", 0), reverse=True)[:limit]
    for row in rows:
        row.pop("_bytes", None)
    return rows


def _du(path: str) -> float | None:
    p = Path(os.path.expanduser(path))
    if not p.exists():
        return None
    rc, out, _ = _run(["du", "-sk", str(p)], timeout=8)
    if rc != 0 or not out.strip():
        return None
    try:
        kb = int(out.split()[0])
        return round(kb / (1024 * 1024), 2)
    except Exception:
        return None


def _glob_size(root: str, name: str, max_depth: int = 3) -> float | None:
    """Approximate total size of all top-level <name> dirs under root, capped depth."""
    root_p = Path(os.path.expanduser(root))
    if not root_p.exists():
        return None
    total_kb = 0
    found_any = False
    # Use find with maxdepth to avoid pathological recursion
    rc, out, _ = _run(
        ["find", str(root_p), "-maxdepth", str(max_depth), "-type", "d", "-name", name, "-prune"],
        timeout=15
    )
    if rc != 0:
        return None
    for line in out.splitlines():
        if not line.strip():
            continue
        rc2, out2, _ = _run(["du", "-sk", line], timeout=4)
        if rc2 == 0 and out2.strip():
            try:
                total_kb += int(out2.split()[0])
                found_any = True
            except Exception:
                pass
    if not found_any:
        return 0.0
    return round(total_kb / (1024 * 1024), 2)


def _docker_disk() -> float | None:
    if not shutil.which("docker"):
        return None
    rc, out, _ = _run(["docker", "system", "df", "--format", "{{.Size}}"], timeout=4)
    if rc != 0:
        return None
    # Sum sizes - rough: parse "1.2GB", "300MB", etc.
    total_gb = 0.0
    for line in out.splitlines():
        m = re.match(r"([\d.]+)\s*([KMGT]?B)", line.strip(), re.IGNORECASE)
        if not m:
            continue
        val, unit = float(m.group(1)), m.group(2).upper()
        mult = {"B": 1e-9, "KB": 1e-6, "MB": 1e-3, "GB": 1.0, "TB": 1024.0}.get(unit, 0)
        total_gb += val * mult
    return round(total_gb, 2)


def outdated_packages() -> dict[str, int | None]:
    """Counts of outdated packages for brew, npm -g, pip."""
    result = {}

    if shutil.which("brew"):
        rc, out, _ = _run(["brew", "outdated"], timeout=8)
        result["brew"] = len([l for l in out.splitlines() if l.strip()]) if rc == 0 else None
    else:
        result["brew"] = None

    if shutil.which("npm"):
        rc, out, _ = _run(["npm", "outdated", "-g", "--json"], timeout=10)
        # npm outdated exits 1 when there are outdated packages
        try:
            import json as _json
            data = _json.loads(out) if out.strip() else {}
            result["npm (global)"] = len(data)
        except Exception:
            result["npm (global)"] = None
    else:
        result["npm (global)"] = None

    if shutil.which("pip3"):
        rc, out, _ = _run(["pip3", "list", "--outdated", "--format=json"], timeout=10)
        try:
            import json as _json
            data = _json.loads(out) if out.strip() else []
            result["pip"] = len(data)
        except Exception:
            result["pip"] = None
    else:
        result["pip"] = None

    return result


def ssh_vpn_sessions() -> dict[str, Any]:
    """Active outbound SSH connections + VPN-ish interfaces (utun, ipsec)."""
    ssh = []
    try:
        for c in psutil.net_connections(kind="tcp"):
            if c.status != psutil.CONN_ESTABLISHED:
                continue
            if c.raddr and c.raddr.port == 22:
                pname = ""
                if c.pid:
                    try:
                        pname = psutil.Process(c.pid).name()
                    except Exception:
                        pname = ""
                ssh.append({"remote": f"{c.raddr.ip}:{c.raddr.port}", "pid": c.pid, "process": pname})
    except (psutil.AccessDenied, RuntimeError):
        pass

    vpn_interfaces = []
    try:
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        for name, iface_stats in stats.items():
            if iface_stats.isup and (name.startswith("utun") or name.startswith("ipsec") or name.startswith("ppp") or name.startswith("tap") or name.startswith("tun")):
                ips = [a.address for a in addrs.get(name, []) if "." in a.address]
                if ips:
                    vpn_interfaces.append({"name": name, "ip": ips[0]})
    except Exception:
        pass

    return {"ssh": ssh, "vpn": vpn_interfaces}


async def dev_server_health(ports: list[int], default_path: str = "/") -> list[dict[str, Any]]:
    """For each listening port, do a quick GET. Status code + response time.
    Bypasses any system proxies so localhost checks always go direct."""
    rows = []
    try:
        client = httpx.AsyncClient(
            timeout=1.0,
            follow_redirects=False,
            trust_env=False,  # ignore HTTP(S)_PROXY/SOCKS env vars
        )
    except Exception:
        return [{"port": p, "status": None, "ok": False} for p in ports]
    try:
        for port in ports:
            url = f"http://localhost:{port}{default_path}"
            try:
                r = await client.get(url)
                rows.append({"port": port, "status": r.status_code, "ok": 200 <= r.status_code < 400})
            except Exception:
                rows.append({"port": port, "status": None, "ok": False})
    finally:
        await client.aclose()
    return rows


def diagnostic(metrics: dict[str, Any]) -> list[str]:
    """Plain-English summary of what's wrong, given the latest metrics blob."""
    findings = []
    cpu = metrics.get("system", {}).get("cpu", {})
    ram = metrics.get("system", {}).get("ram", {})
    disk_data = metrics.get("disk", {}).get("partitions", [])
    pressure = ram.get("pressure", {}) or {}
    top_ram = metrics.get("system", {}).get("top_ram", []) or []
    top_cpu = metrics.get("system", {}).get("top_cpu", []) or []
    net = metrics.get("net", {})

    if cpu.get("overall_pct", 0) > 85:
        offender = top_cpu[0] if top_cpu else None
        msg = f"CPU is hot at {cpu['overall_pct']:.0f}%."
        if offender:
            msg += f" Biggest user: {offender['name']} (PID {offender['pid']}) at {offender['cpu_pct']:.0f}%."
        findings.append(msg)

    if pressure.get("level") == "red":
        findings.append(f"Memory pressure is RED ({pressure.get('free_pct')}% free). macOS may start killing apps.")
    elif pressure.get("level") == "yellow":
        findings.append(f"Memory pressure is yellow ({pressure.get('free_pct')}% free). Things will start to compress.")

    pressure_level = pressure.get("level")
    available_gb = ram.get("available_gb", 0)
    if ram.get("swap_pct", 0) > 50 and (pressure_level in {"yellow", "red"} or available_gb < 2):
        findings.append(f"Heavy swap use ({ram['swap_pct']:.0f}%). Machine is slower than it should be.")

    if top_ram:
        biggest = top_ram[0]
        if biggest["rss_mb"] > 4000:
            findings.append(f"{biggest['name']} (PID {biggest['pid']}) is holding {biggest['rss_mb']/1024:.1f}GB of RAM.")

    for part in disk_data:
        if part.get("pct", 0) >= 90:
            findings.append(f"{part['mount']} is {part['pct']:.0f}% full ({part['free_gb']:.0f}GB free).")

    inet = metrics.get("internet", {}) or {}
    for k, v in inet.items():
        if v is False:
            findings.append(f"Network: {k} is unreachable.")

    if not findings:
        findings.append("All clear. CPU, RAM, disk, and network look healthy.")
    return findings


def cheatsheet() -> list[dict[str, str]]:
    """Static reference of useful commands. Click a row to run it in Terminal."""
    return [
        {"cmd": "df -h", "desc": "Disk free, human-readable"},
        {"cmd": "du -sh ~/* 2>/dev/null | sort -h | tail -20", "desc": "Largest home folders"},
        {"cmd": "du -sh ~/Library/Application\\ Support/* 2>/dev/null | sort -h | tail -20", "desc": "Largest app support folders"},
        {"cmd": "du -sh ~/Library/Caches ~/.cache 2>/dev/null", "desc": "Cache sizes"},
        {"cmd": "tmutil listlocalsnapshots /", "desc": "Time Machine snapshots"},
        {"cmd": "lsof -nP -iTCP -sTCP:LISTEN", "desc": "All listening ports"},
        {"cmd": "netstat -vanp tcp | grep LISTEN", "desc": "Low-level TCP listeners"},
        {"cmd": "ps aux | sort -nrk 3,3 | head -15", "desc": "Top CPU processes"},
        {"cmd": "ps aux | sort -nrk 4,4 | head -15", "desc": "Top RAM processes"},
        {"cmd": "vm_stat", "desc": "Virtual memory stats"},
        {"cmd": "memory_pressure", "desc": "macOS memory pressure"},
        {"cmd": "uptime", "desc": "Load average and uptime"},
        {"cmd": "pmset -g batt", "desc": "Battery info"},
        {"cmd": "ping -c 4 1.1.1.1", "desc": "Internet ping test"},
        {"cmd": "dig github.com", "desc": "DNS lookup test"},
        {"cmd": "curl -I https://github.com", "desc": "HTTPS reachability"},
        {"cmd": "scutil --dns | head -60", "desc": "DNS configuration"},
        {"cmd": "ifconfig | grep '^[a-z].*:'", "desc": "Network interfaces"},
        {"cmd": "brew services list", "desc": "Background services"},
        {"cmd": "brew outdated", "desc": "Outdated Homebrew packages"},
        {"cmd": "brew cleanup --dry-run", "desc": "Homebrew cleanup preview"},
        {"cmd": "brew doctor", "desc": "Homebrew health check"},
        {"cmd": "docker ps -a", "desc": "All containers (running + stopped)"},
        {"cmd": "docker system df", "desc": "Docker disk usage"},
        {"cmd": "docker stats --no-stream", "desc": "Container CPU/RAM now"},
        {"cmd": "git -C ~/sysdash status --short", "desc": "sysdash git status"},
        {"cmd": "git -C ~/sysdash log --oneline -10", "desc": "sysdash recent commits"},
        {"cmd": "python3 --version", "desc": "Python version"},
        {"cmd": "node --version && npm --version", "desc": "Node and npm versions"},
        {"cmd": "xcode-select -p", "desc": "Active Xcode path"},
        {"cmd": "xcrun simctl list runtimes", "desc": "Simulator runtimes"},
        {"cmd": "launchctl print gui/$(id -u)/com.sysdash.agent | head -80", "desc": "sysdash LaunchAgent"},
        {"cmd": "cat ~/.sysdash-port", "desc": "Current sysdash port"},
        {"cmd": "curl -s http://127.0.0.1:55067/api/snapshot | python3 -m json.tool | head -80", "desc": "sysdash snapshot"},
        {"cmd": "tail -n 80 ~/sysdash/sysdash.log", "desc": "sysdash log"},
        {"cmd": "tail -n 80 ~/sysdash/sysdash.err", "desc": "sysdash errors"},
    ]
