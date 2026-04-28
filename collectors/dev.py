"""Dev signals: ports, Docker, services, runtimes, git, dev-server health."""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import psutil


def _run(cmd: list[str], timeout: float = 3.0) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except Exception:
        return ""


def _decode_lsof_command(name: str) -> str:
    return name.replace("\\x20", " ").strip()


def _parse_lsof_tcp_line(line: str) -> dict[str, Any] | None:
    parts = line.split()
    if len(parts) < 9 or parts[7] != "TCP":
        return None

    endpoint = " ".join(parts[8:])
    status = ""
    m_status = re.search(r"\(([^)]+)\)\s*$", endpoint)
    if m_status:
        status = m_status.group(1)
        endpoint = endpoint[:m_status.start()].strip()

    local = endpoint
    remote = ""
    if "->" in endpoint:
        local, remote = endpoint.split("->", 1)

    def port_from(value: str) -> int | None:
        m = re.search(r":(\d+)$", value.strip())
        return int(m.group(1)) if m else None

    try:
        pid = int(parts[1])
    except ValueError:
        pid = None

    return {
        "process": _decode_lsof_command(parts[0]),
        "pid": pid,
        "local": local.strip(),
        "remote": remote.strip(),
        "lport": port_from(local),
        "rport": port_from(remote),
        "status": status,
    }


def _lsof_tcp_rows(state: str | None = None) -> list[dict[str, Any]]:
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    cmd = [lsof, "-nP", "-iTCP"]
    if state:
        cmd.append(f"-sTCP:{state}")
    out = _run(cmd, timeout=5)
    rows = []
    for line in out.splitlines()[1:]:
        row = _parse_lsof_tcp_line(line)
        if row:
            rows.append(row)
    return rows


def listening_ports() -> list[dict[str, Any]]:
    """Listening TCP ports + owning process. Uses psutil.net_connections."""
    rows = []
    try:
        conns = psutil.net_connections(kind="tcp")
    except (psutil.AccessDenied, PermissionError, RuntimeError):
        conns = []
    for c in conns:
        if c.status != psutil.CONN_LISTEN:
            continue
        if not c.laddr:
            continue
        port = c.laddr.port
        pid = c.pid
        name = "?"
        if pid:
            try:
                name = psutil.Process(pid).name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                name = "?"
        rows.append({"port": port, "pid": pid, "process": name, "addr": c.laddr.ip})
    # dedupe by (port, pid) - many bindings on 0.0.0.0 + ::
    seen = set()
    uniq = []
    for r in rows:
        k = (r["port"], r["pid"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    if not uniq:
        for row in _lsof_tcp_rows("LISTEN"):
            port = row.get("lport")
            if not port:
                continue
            uniq.append({
                "port": port,
                "pid": row.get("pid"),
                "process": row.get("process") or "?",
                "addr": row.get("local", "").rsplit(":", 1)[0],
            })

    seen = set()
    deduped = []
    for row in uniq:
        key = (row["port"], row["pid"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    return sorted(deduped, key=lambda x: x["port"])


def active_ports() -> list[dict[str, Any]]:
    """Established TCP connections with local/remote ports and owning process."""
    rows = []
    try:
        conns = psutil.net_connections(kind="tcp")
    except (psutil.AccessDenied, PermissionError, RuntimeError):
        conns = []

    for c in conns:
        if c.status != psutil.CONN_ESTABLISHED or not c.laddr or not c.raddr:
            continue
        pid = c.pid
        name = "?"
        if pid:
            try:
                name = psutil.Process(pid).name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                name = "?"
        rows.append({
            "local": f"{c.laddr.ip}:{c.laddr.port}",
            "remote": f"{c.raddr.ip}:{c.raddr.port}",
            "lport": c.laddr.port,
            "rport": c.raddr.port,
            "pid": pid,
            "process": name,
        })

    if not rows:
        for row in _lsof_tcp_rows("ESTABLISHED"):
            if not row.get("local") or not row.get("remote"):
                continue
            rows.append({
                "local": row["local"],
                "remote": row["remote"],
                "lport": row.get("lport"),
                "rport": row.get("rport"),
                "pid": row.get("pid"),
                "process": row.get("process") or "?",
            })

    seen = set()
    deduped = []
    for row in rows:
        key = (row["pid"], row["local"], row["remote"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    return sorted(deduped, key=lambda x: (x["process"], x["rport"] or 0, x["remote"]))[:80]


def docker_containers() -> list[dict[str, Any]]:
    if not shutil.which("docker"):
        return []
    out = _run([
        "docker", "ps", "-a",
        "--format", "{{json .}}"
    ], timeout=4)
    rows = []
    for line in out.splitlines():
        try:
            d = json.loads(line)
            rows.append({
                "id": d.get("ID", "")[:12],
                "name": d.get("Names", ""),
                "image": d.get("Image", ""),
                "status": d.get("Status", ""),
                "ports": d.get("Ports", ""),
                "running": d.get("State", "") == "running" or "Up" in d.get("Status", ""),
            })
        except json.JSONDecodeError:
            continue
    return rows


def brew_services() -> list[dict[str, Any]]:
    if not shutil.which("brew"):
        return []
    out = _run(["brew", "services", "list"], timeout=4)
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        name, status = parts[0], parts[1]
        rows.append({"name": name, "status": status})
    return rows


def detect_runtimes() -> dict[str, Any]:
    """Find active node/python/etc processes with their PID + memory."""
    interesting = ("node", "python", "python3", "ruby", "go", "java", "deno", "bun", "rails")
    found = []
    for p in psutil.process_iter(["pid", "name", "memory_info", "cmdline"]):
        try:
            n = (p.info["name"] or "").lower()
            if n in interesting:
                cmd = " ".join(p.info["cmdline"] or [])[:80]
                rss = p.info["memory_info"].rss if p.info["memory_info"] else 0
                found.append({
                    "pid": p.info["pid"],
                    "runtime": n,
                    "cmd": cmd,
                    "rss_mb": round(rss / (1024**2), 1),
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return {"processes": sorted(found, key=lambda x: -x["rss_mb"])[:15]}


LLM_SERVERS = [
    {"name": "Ollama", "port": 11434, "models": "/api/tags", "kind": "ollama"},
    {"name": "LM Studio", "port": 1234, "models": "/v1/models", "kind": "openai"},
    {"name": "llama.cpp", "port": 8080, "models": "/v1/models", "kind": "openai"},
    {"name": "text-generation-webui", "port": 5000, "models": "/v1/models", "kind": "openai"},
    {"name": "KoboldCPP", "port": 5001, "models": "/api/v1/model", "kind": "kobold"},
]

_LLM_TPS_STATE: dict[int, tuple[float, float]] = {}
_LLM_SAMPLE_STATE: dict[int, dict[str, Any]] = {}


def _http_json(url: str, timeout: float = 1.4) -> tuple[bool, Any]:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(200_000).decode("utf-8", errors="replace")
        return True, json.loads(raw) if raw else {}
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return False, None


def _http_post_json(url: str, payload: dict[str, Any], timeout: float = 18.0) -> tuple[bool, Any, float]:
    started = time.time()
    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(300_000).decode("utf-8", errors="replace")
        return True, json.loads(raw) if raw else {}, max(time.time() - started, 0.001)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return False, None, max(time.time() - started, 0.001)


def _http_text(url: str, timeout: float = 0.35) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, headers={"Accept": "text/plain"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(300_000).decode("utf-8", errors="replace")
        return True, raw
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return False, ""


def _llm_model_names(kind: str, data: Any) -> list[str]:
    if not data:
        return []
    if kind == "ollama":
        return [str(x.get("name") or x.get("model")) for x in data.get("models", []) if x.get("name") or x.get("model")]
    if kind == "openai":
        return [str(x.get("id")) for x in data.get("data", []) if x.get("id")]
    if kind == "kobold":
        if isinstance(data, dict):
            name = data.get("result") or data.get("model") or data.get("name")
            return [str(name)] if name else []
    return []


def _ollama_loaded_models(port: int) -> list[str]:
    ok, data = _http_json(f"http://127.0.0.1:{port}/api/ps", timeout=1.4)
    if not ok or not isinstance(data, dict):
        return []
    return [str(x.get("name") or x.get("model")) for x in data.get("models", []) if x.get("name") or x.get("model")]


def _parse_prom_metrics(text: str) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.rsplit(None, 1)
        if len(parts) != 2:
            continue
        name = parts[0].split("{", 1)[0]
        try:
            value = float(parts[1])
        except ValueError:
            continue
        metrics[name] = metrics.get(name, 0.0) + value
    return metrics


def _llm_tokens_per_sec(port: int) -> dict[str, Any]:
    ok, text = _http_text(f"http://127.0.0.1:{port}/metrics")
    if not ok or not text:
        return {"value": None, "source": ""}

    metrics = _parse_prom_metrics(text)
    direct = [
        value for name, value in metrics.items()
        if any(token in name.lower() for token in ("tokens_per_second", "tokens_per_sec", "tok_per_sec", "tps"))
    ]
    if direct:
        return {"value": round(max(direct), 2), "source": "metrics"}

    generated_names = []
    for name in metrics:
        low = name.lower()
        if "token" not in low or "total" not in low:
            continue
        if any(skip in low for skip in ("prompt", "input", "duration", "seconds", "latency")):
            continue
        if any(hit in low for hit in ("generated", "predicted", "completion", "output", "eval")):
            generated_names.append(name)

    total = sum(metrics[name] for name in generated_names)
    if total <= 0:
        return {"value": None, "source": "metrics"}

    now = time.time()
    prev = _LLM_TPS_STATE.get(port)
    _LLM_TPS_STATE[port] = (now, total)
    if not prev:
        return {"value": None, "source": "warming"}
    prev_ts, prev_total = prev
    dt = max(now - prev_ts, 0.001)
    if total < prev_total:
        return {"value": None, "source": "reset"}
    return {"value": round((total - prev_total) / dt, 2), "source": "metrics"}


def _cache_llm_sample(port: int, value: float) -> dict[str, Any]:
    state = _LLM_SAMPLE_STATE.setdefault(port, {"ts": 0.0, "values": []})
    values = [*state.get("values", []), float(value)][-8:]
    state["ts"] = time.time()
    state["values"] = values
    return {"value": round(sum(values) / len(values), 2), "source": "avg sample", "samples": len(values)}


def _cached_llm_sample(port: int, max_age: float = 120.0) -> dict[str, Any] | None:
    state = _LLM_SAMPLE_STATE.get(port)
    if not state or not state.get("values"):
        return None
    values = state["values"]
    if time.time() - float(state.get("ts", 0)) <= max_age:
        return {"value": round(sum(values) / len(values), 2), "source": "avg sample", "samples": len(values)}
    return None


def _sample_llm_tokens_per_sec(spec: dict[str, Any], models: list[str]) -> dict[str, Any]:
    port = int(spec["port"])
    cached = _cached_llm_sample(port)
    if cached:
        return cached
    if not models:
        return {"value": None, "source": ""}

    model = models[0]
    prompt = "Reply with one short sentence about local development."
    if spec["kind"] == "ollama":
        ok, data, _elapsed = _http_post_json(
            f"http://127.0.0.1:{port}/api/generate",
            {
                "model": model,
                "prompt": prompt,
                "stream": False,
                "keep_alive": "5m",
                "options": {"num_predict": 24, "temperature": 0},
            },
            timeout=24.0,
        )
        if ok and isinstance(data, dict):
            count = data.get("eval_count") or data.get("completion_tokens")
            duration = data.get("eval_duration")
            if count and duration:
                return _cache_llm_sample(port, float(count) / (float(duration) / 1_000_000_000))

    if spec["kind"] == "openai":
        ok, data, elapsed = _http_post_json(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 24,
                "temperature": 0,
                "stream": False,
            },
            timeout=24.0,
        )
        if ok and isinstance(data, dict):
            usage = data.get("usage") or {}
            count = usage.get("completion_tokens")
            if count:
                return _cache_llm_sample(port, float(count) / elapsed)

    return {"value": None, "source": "sample unavailable"}


def local_llms() -> dict[str, Any]:
    """Known local LLM servers + matching processes."""
    listeners = {row["port"]: row for row in listening_ports()}
    servers = []
    for spec in LLM_SERVERS:
        listener = listeners.get(spec["port"])
        url = f"http://127.0.0.1:{spec['port']}{spec['models']}"
        ok, data = _http_json(url)
        models = _llm_model_names(spec["kind"], data)
        if spec["kind"] == "ollama" and not models:
            models = _ollama_loaded_models(spec["port"])
        tps = _llm_tokens_per_sec(spec["port"]) if ok or listener else {"value": None, "source": ""}
        if tps["value"] is None and ok:
            tps = _sample_llm_tokens_per_sec(spec, models)
        servers.append({
            "name": spec["name"],
            "port": spec["port"],
            "status": "api" if ok else "listening" if listener else "offline",
            "pid": listener.get("pid") if listener else None,
            "process": listener.get("process") if listener else "",
            "models": models[:6],
            "model_count": len(models),
            "url": url,
            "tokens_per_sec": tps["value"],
            "tps_source": tps["source"],
            "tps_samples": tps.get("samples", 0),
        })

    needles = (
        "ollama", "lm studio", "lmstudio", "llama", "kobold", "text-generation",
        "oobabooga", "mlx", "vllm", "transformers", "llamafile"
    )
    processes = []
    try:
        proc_iter = psutil.process_iter(["pid", "name", "memory_info", "cpu_percent", "cmdline"])
        for p in proc_iter:
            name = p.info.get("name") or ""
            cmd = " ".join(p.info.get("cmdline") or [])
            haystack = f"{name} {cmd}".lower()
            if not any(n in haystack for n in needles):
                continue
            rss = p.info["memory_info"].rss if p.info.get("memory_info") else 0
            processes.append({
                "pid": p.info["pid"],
                "name": name[:34],
                "rss_mb": round(rss / (1024**2), 1),
                "cpu_pct": round(p.info.get("cpu_percent") or 0.0, 1),
                "cmd": cmd[:140],
            })
    except (psutil.NoSuchProcess, psutil.AccessDenied, PermissionError, RuntimeError):
        processes = []

    active = [s for s in servers if s["status"] != "offline"]
    return {
        "servers": servers,
        "processes": sorted(processes, key=lambda x: -x["rss_mb"])[:10],
        "summary": {
            "active_servers": len(active),
            "model_count": sum(s["model_count"] for s in servers),
            "ram_mb": round(sum(p["rss_mb"] for p in processes), 1),
            "tokens_per_sec": round(sum(s["tokens_per_sec"] or 0 for s in servers), 2),
        },
    }


def toolchain_versions() -> list[dict[str, Any]]:
    tools = [
        ("node", ["node", "--version"]),
        ("npm", ["npm", "--version"]),
        ("python", ["python3", "--version"]),
        ("pip", ["pip3", "--version"]),
        ("ruby", ["ruby", "--version"]),
        ("go", ["go", "version"]),
        ("java", ["java", "-version"]),
        ("git", ["git", "--version"]),
        ("docker", ["docker", "--version"]),
    ]
    out = []
    for label, cmd in tools:
        if not shutil.which(cmd[0]):
            out.append({"tool": label, "version": "(not installed)", "path": ""})
            continue
        path = shutil.which(cmd[0]) or ""
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
            v = (r.stdout + r.stderr).strip().splitlines()[0] if (r.stdout + r.stderr).strip() else "?"
        except Exception:
            v = "?"
        out.append({"tool": label, "version": v, "path": path})
    return out


def package_inventory() -> list[dict[str, Any]]:
    """Installed packages with installed/latest versions where the package manager exposes them."""
    rows: list[dict[str, Any]] = []
    rows.extend(_brew_packages())
    rows.extend(_npm_global_packages())
    rows.extend(_pip_packages())
    return sorted(rows, key=lambda x: (x["manager"], x["name"].lower()))


def _brew_packages() -> list[dict[str, Any]]:
    if not shutil.which("brew"):
        return []

    installed: list[dict[str, Any]] = []
    out = _run(["brew", "list", "--versions"], timeout=10)
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        installed.append({
            "manager": "brew",
            "name": parts[0],
            "version": " ".join(parts[1:]),
            "latest": "",
            "status": "unknown",
        })

    outdated: dict[str, str] = {}
    raw = _run(["brew", "outdated", "--json=v2"], timeout=20)
    try:
        data = json.loads(raw) if raw.strip() else {}
        for item in data.get("formulae", []):
            versions = item.get("current_versions") or []
            outdated[item.get("name", "")] = item.get("current_version") or ", ".join(versions)
        for item in data.get("casks", []):
            outdated[item.get("name", "")] = item.get("current_version") or ""
    except Exception:
        outdated = {}

    for row in installed:
        if row["name"] in outdated:
            row["latest"] = outdated[row["name"]]
            row["status"] = "outdated"
        else:
            row["latest"] = row["version"]
            row["status"] = "current"
    return installed


def _npm_global_packages() -> list[dict[str, Any]]:
    if not shutil.which("npm"):
        return []

    installed: list[dict[str, Any]] = []
    raw = _run(["npm", "ls", "-g", "--depth=0", "--json"], timeout=12)
    try:
        data = json.loads(raw) if raw.strip() else {}
        for name, meta in (data.get("dependencies") or {}).items():
            installed.append({
                "manager": "npm",
                "name": name,
                "version": meta.get("version", ""),
                "latest": "",
                "status": "unknown",
            })
    except Exception:
        return installed

    outdated: dict[str, str] = {}
    raw = _run(["npm", "outdated", "-g", "--json"], timeout=20)
    try:
        data = json.loads(raw) if raw.strip() else {}
        for name, meta in data.items():
            outdated[name] = str(meta.get("latest") or meta.get("wanted") or "")
    except Exception:
        outdated = {}

    for row in installed:
        if row["name"] in outdated:
            row["latest"] = outdated[row["name"]]
            row["status"] = "outdated"
        else:
            row["latest"] = row["version"]
            row["status"] = "current"
    return installed


def _pip_packages() -> list[dict[str, Any]]:
    pip = shutil.which("pip3") or shutil.which("pip")
    if not pip:
        return []

    installed: list[dict[str, Any]] = []
    raw = _run([pip, "list", "--format=json"], timeout=12)
    try:
        data = json.loads(raw) if raw.strip() else []
        for item in data:
            installed.append({
                "manager": "pip",
                "name": item.get("name", ""),
                "version": item.get("version", ""),
                "latest": "",
                "status": "unknown",
            })
    except Exception:
        return installed

    outdated: dict[str, str] = {}
    raw = _run([pip, "list", "--outdated", "--format=json"], timeout=20)
    try:
        data = json.loads(raw) if raw.strip() else []
        for item in data:
            outdated[item.get("name", "")] = item.get("latest_version", "")
    except Exception:
        outdated = {}

    for row in installed:
        if row["name"] in outdated:
            row["latest"] = outdated[row["name"]]
            row["status"] = "outdated"
        else:
            row["latest"] = row["version"]
            row["status"] = "current"
    return installed


def git_status_for_repos(repo_paths: list[str]) -> list[dict[str, Any]]:
    results = []
    for raw in repo_paths:
        root = Path(os.path.expanduser(raw))
        if not root.exists() or not root.is_dir():
            continue
        # Scan one level deep for .git directories
        candidates = [root] + [c for c in root.iterdir() if c.is_dir()]
        for c in candidates:
            git_dir = c / ".git"
            if not git_dir.exists():
                continue
            try:
                branch = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=c, capture_output=True, text=True, timeout=2
                ).stdout.strip()
                porcelain = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=c, capture_output=True, text=True, timeout=2
                ).stdout
                dirty = bool(porcelain.strip())
                ahead_behind = subprocess.run(
                    ["git", "rev-list", "--left-right", "--count", f"@{{u}}...HEAD"],
                    cwd=c, capture_output=True, text=True, timeout=2
                ).stdout.strip()
                behind, ahead = (0, 0)
                if ahead_behind:
                    parts = ahead_behind.split()
                    if len(parts) == 2:
                        behind, ahead = int(parts[0]), int(parts[1])
                results.append({
                    "path": str(c),
                    "name": c.name,
                    "branch": branch,
                    "dirty": dirty,
                    "ahead": ahead,
                    "behind": behind,
                })
            except Exception:
                continue
    # Dedupe by path
    seen = set()
    uniq = []
    for r in results:
        if r["path"] in seen:
            continue
        seen.add(r["path"])
        uniq.append(r)
    return uniq
