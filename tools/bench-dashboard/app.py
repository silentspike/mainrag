#!/usr/bin/env python3
"""
MainRAG Benchmark Dashboard Service

Features:
- Start/stop benchmarks
- Live telemetry streaming (SSE)
- Persistent run storage for comparisons
- Preflight checks before each run

This service is intentionally self-contained and uses only standard
library networking for benchmark calls to avoid extra dependencies.
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import random
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib import request as urlrequest
from urllib.error import URLError, HTTPError

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# -----------------------------------------------------------------------------
# Paths and defaults
# -----------------------------------------------------------------------------

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parents[1]
DATA_DIR = REPO_ROOT / "data" / "benchmarks"
STATIC_DIR = APP_DIR / "static"
DATA_DIR.mkdir(parents=True, exist_ok=True)

INDEX_FILE = DATA_DIR / "index.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def load_default_token() -> Optional[str]:
    # Try standard config locations for mainrag token
    candidates = [
        Path.home() / ".config" / "mainrag" / "token",
        Path("/root/.config/mainrag/token"),
    ]
    for path in candidates:
        try:
            if path.exists():
                token = path.read_text().strip()
                if token:
                    return token
        except Exception:
            continue
    return None

def load_env_file(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    data: Dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        data[key.strip()] = val.strip()
    return data


def build_default_urls() -> Dict[str, str]:
    env = load_env_file(REPO_ROOT / "mainrag.env")

    host = env.get("API_HOST", "localhost")
    port = env.get("API_PORT", "3001")
    if host in ("0.0.0.0", "127.0.0.1"):
        host = "localhost"
    api_url = f"http://{host}:{port}"

    qdrant_url = env.get("QDRANT_REST_URL", "http://localhost:6333")
    qdrant_api_key = env.get("QDRANT_API_KEY", "")
    tei_url = env.get("TEI_URL", "http://localhost:8080")
    rerank_url = env.get("TEI_RERANKER_URL", "http://localhost:8082")
    ocr_url = env.get("OCR_SERVICE_URL", "http://localhost:8090")
    ocr_enabled = env.get("OCR_ENABLED", "false").lower() in ("1", "true", "yes")

    default_token = load_default_token()
    return {
        "api_url": api_url,
        "qdrant_url": qdrant_url,
        "qdrant_api_key": qdrant_api_key,
        "tei_url": tei_url,
        "rerank_url": rerank_url,
        "ocr_url": ocr_url,
        "ocr_enabled": "true" if ocr_enabled else "false",
        "token_available": "true" if default_token else "false",
    }


DEFAULTS = build_default_urls()

# -----------------------------------------------------------------------------
# Benchmark definitions
# -----------------------------------------------------------------------------

BENCHMARKS = [
    {
        "id": "system_pulse",
        "name": "System Pulse Benchmark",
        "summary": "A safe, read-only benchmark that validates dashboard functionality and \n" \
                   "collects live system and API telemetry.",
        "details": [
            "Runs preflight checks against API, Qdrant, TEI, Reranker, and OCR (if enabled).",
            "Collects CPU, RAM, disk IO, IO wait, queue depth, and optional GPU stats.",
            "Measures API /health and /metrics latency; optional search smoke test if token provided.",
            "Designed to be restart-safe: no data is written to MainRAG." 
        ],
        "defaults": {
            "duration_sec": 60,
            "interval_ms": 1000,
            "api_url": DEFAULTS["api_url"],
        }
    },
    {
        "id": "search_latency",
        "name": "Search Latency Benchmark",
        "summary": "Measures authenticated search latency and throughput under a controlled query mix.",
        "details": [
            "Requires a valid Bearer token to access /api/v1/search.",
            "Executes a rotating query set with configurable concurrency and quality tier.",
            "Streams per-interval latency and error counts, plus system telemetry.",
            "Read-only and safe to rerun. No data mutations.",
        ],
        "defaults": {
            "duration_sec": 60,
            "interval_ms": 1000,
            "concurrency": 4,
            "limit": 10,
            "quality": "fast",
            "api_url": DEFAULTS["api_url"],
        }
    },
    {
        "id": "search_ramp",
        "name": "Search Ramp Benchmark",
        "summary": "Progressively increases concurrency to reveal search scaling behavior and saturation points.",
        "details": [
            "Requires a valid Bearer token to access /api/v1/search.",
            "Runs in stages; each stage increases concurrency by a fixed multiplier.",
            "Captures stage-by-stage latency, throughput, and error rates.",
            "Read-only and safe to rerun. No data mutations.",
        ],
        "defaults": {
            "stage_duration_sec": 20,
            "stages": 4,
            "concurrency": 2,
            "interval_ms": 1000,
            "limit": 10,
            "quality": "fast",
            "api_url": DEFAULTS["api_url"],
        }
    }
]

# -----------------------------------------------------------------------------
# System telemetry helpers
# -----------------------------------------------------------------------------


def detect_root_device() -> Optional[str]:
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "/":
                    dev = parts[0]
                    if not dev.startswith("/dev/"):
                        return None
                    name = dev.split("/")[-1]
                    # nvme0n1p2 -> nvme0n1
                    if name.startswith("nvme") and "p" in name:
                        return name.split("p")[0]
                    # mmcblk0p1 -> mmcblk0
                    if name.startswith("mmcblk") and "p" in name:
                        return name.split("p")[0]
                    # sda1 -> sda
                    return re.sub(r"\d+$", "", name)
    except OSError:
        return None
    return None


def read_cpu_stat() -> Optional[Dict[str, int]]:
    try:
        with open("/proc/stat", "r", encoding="utf-8") as f:
            line = f.readline()
        if not line.startswith("cpu "):
            return None
        parts = line.split()
        values = list(map(int, parts[1:]))
        keys = ["user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"]
        stats = dict(zip(keys, values))
        total = sum(values)
        stats["total"] = total
        return stats
    except OSError:
        return None


def read_meminfo() -> Optional[Dict[str, int]]:
    try:
        mem = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    mem[parts[0].rstrip(":")] = int(parts[1])
        return mem
    except OSError:
        return None


def read_diskstats(device: str) -> Optional[Dict[str, int]]:
    try:
        with open("/proc/diskstats", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 14:
                    continue
                if parts[2] == device:
                    return {
                        "reads": int(parts[3]),
                        "reads_merged": int(parts[4]),
                        "sectors_read": int(parts[5]),
                        "read_ms": int(parts[6]),
                        "writes": int(parts[7]),
                        "writes_merged": int(parts[8]),
                        "sectors_written": int(parts[9]),
                        "write_ms": int(parts[10]),
                        "io_in_progress": int(parts[11]),
                        "io_ms": int(parts[12]),
                        "io_weighted_ms": int(parts[13]),
                    }
    except OSError:
        return None
    return None


def get_gpu_stats() -> Optional[Dict[str, float]]:
    cmd = [
        "nvidia-smi",
        "--query-gpu=utilization.gpu,memory.used,memory.total",
        "--format=csv,noheader,nounits",
    ]
    try:
        out = subprocess.check_output(cmd, timeout=1).decode("utf-8").strip()
        if not out:
            return None
        util_str, mem_used_str, mem_total_str = [p.strip() for p in out.split(",")]
        util = float(util_str)
        mem_used = float(mem_used_str)
        mem_total = float(mem_total_str)
        mem_pct = (mem_used / mem_total) * 100.0 if mem_total > 0 else 0.0
        return {
            "gpu_util": util,
            "gpu_mem_used": mem_used,
            "gpu_mem_total": mem_total,
            "gpu_mem_pct": mem_pct,
        }
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


class SystemSampler:
    def __init__(self) -> None:
        self.root_dev = detect_root_device()
        self.prev_cpu = read_cpu_stat()
        self.prev_disk = read_diskstats(self.root_dev) if self.root_dev else None
        self.prev_time = time.time()

    def sample(self) -> Dict[str, Optional[float]]:
        now = time.time()
        dt = max(now - self.prev_time, 1e-6)

        cpu = read_cpu_stat()
        mem = read_meminfo()
        disk = read_diskstats(self.root_dev) if self.root_dev else None

        cpu_usage = None
        iowait_pct = None
        if cpu and self.prev_cpu:
            totald = cpu["total"] - self.prev_cpu["total"]
            idled = (cpu["idle"] + cpu["iowait"]) - (self.prev_cpu["idle"] + self.prev_cpu["iowait"])
            totald = max(totald, 1)
            cpu_usage = 100.0 * (1.0 - (idled / totald))
            iowait_pct = 100.0 * ((cpu["iowait"] - self.prev_cpu["iowait"]) / totald)

        mem_total = None
        mem_used_pct = None
        if mem and "MemTotal" in mem and "MemAvailable" in mem:
            mem_total = mem["MemTotal"]
            mem_avail = mem["MemAvailable"]
            mem_used_pct = 100.0 * (1.0 - (mem_avail / mem_total))

        read_mb_s = None
        write_mb_s = None
        iops = None
        queue_depth = None
        if disk and self.prev_disk:
            sector_size = 512
            read_bytes = (disk["sectors_read"] - self.prev_disk["sectors_read"]) * sector_size
            write_bytes = (disk["sectors_written"] - self.prev_disk["sectors_written"]) * sector_size
            read_mb_s = (read_bytes / (1024 * 1024)) / dt
            write_mb_s = (write_bytes / (1024 * 1024)) / dt
            reads = disk["reads"] - self.prev_disk["reads"]
            writes = disk["writes"] - self.prev_disk["writes"]
            iops = (reads + writes) / dt
            queue_depth = float(disk["io_in_progress"])

        gpu = get_gpu_stats()

        self.prev_cpu = cpu
        self.prev_disk = disk
        self.prev_time = now

        return {
            "cpu_pct": cpu_usage,
            "iowait_pct": iowait_pct,
            "mem_used_pct": mem_used_pct,
            "read_mb_s": read_mb_s,
            "write_mb_s": write_mb_s,
            "iops": iops,
            "queue_depth": queue_depth,
            "gpu_util": gpu["gpu_util"] if gpu else None,
            "gpu_mem_pct": gpu["gpu_mem_pct"] if gpu else None,
            "gpu_mem_used": gpu["gpu_mem_used"] if gpu else None,
            "gpu_mem_total": gpu["gpu_mem_total"] if gpu else None,
        }


# -----------------------------------------------------------------------------
# HTTP helpers
# -----------------------------------------------------------------------------


def http_get(url: str, timeout: float = 5.0, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    start = time.time()
    req = urlrequest.Request(url, method="GET")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            _ = resp.read()
            return {"ok": True, "status": resp.status, "ms": (time.time() - start) * 1000.0}
    except HTTPError as e:
        return {"ok": False, "status": e.code, "error": str(e), "ms": (time.time() - start) * 1000.0}
    except URLError as e:
        return {"ok": False, "status": None, "error": str(e), "ms": (time.time() - start) * 1000.0}


def http_post_json(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None, timeout: float = 10.0) -> Dict[str, Any]:
    start = time.time()
    data = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return {
                "ok": True,
                "status": resp.status,
                "ms": (time.time() - start) * 1000.0,
                "body": body.decode("utf-8", errors="ignore"),
            }
    except HTTPError as e:
        return {"ok": False, "status": e.code, "error": str(e), "ms": (time.time() - start) * 1000.0}
    except URLError as e:
        return {"ok": False, "status": None, "error": str(e), "ms": (time.time() - start) * 1000.0}


# -----------------------------------------------------------------------------
# Run storage
# -----------------------------------------------------------------------------


def load_index() -> List[Dict[str, Any]]:
    if not INDEX_FILE.exists():
        return []
    try:
        return json.loads(INDEX_FILE.read_text())
    except Exception:
        return []


def save_index(items: List[Dict[str, Any]]) -> None:
    tmp = INDEX_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2))
    tmp.replace(INDEX_FILE)


def append_jsonl(path: Path, data: Dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data) + "\n")


# -----------------------------------------------------------------------------
# Run state
# -----------------------------------------------------------------------------


@dataclass
class RunState:
    run_id: str
    benchmark_id: str
    status: str
    created_at: str
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    config: Dict[str, Any] = field(default_factory=dict)
    preflight: List[Dict[str, Any]] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    queue: "queue.Queue[Dict[str, Any]]" = field(default_factory=queue.Queue)

    def to_dict(self, include_runtime: bool = False) -> Dict[str, Any]:
        data = {
            "run_id": self.run_id,
            "benchmark_id": self.benchmark_id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "config": self.config,
            "preflight": self.preflight,
            "summary": self.summary,
            "error": self.error,
        }
        if include_runtime:
            data["runtime_sec"] = self.runtime_seconds()
        return data

    def runtime_seconds(self) -> Optional[float]:
        if not self.started_at:
            return None
        end_time = self.ended_at
        try:
            start_ts = datetime.fromisoformat(self.started_at).timestamp()
            if end_time:
                end_ts = datetime.fromisoformat(end_time).timestamp()
            else:
                end_ts = time.time()
            return end_ts - start_ts
        except Exception:
            return None


ACTIVE_RUNS: Dict[str, RunState] = {}
ACTIVE_LOCK = threading.Lock()


def persist_run_state(run: RunState, run_dir: Path) -> None:
    run_file = run_dir / "run.json"
    tmp = run_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(run.to_dict(include_runtime=True), indent=2))
    tmp.replace(run_file)


def update_index_for_run(run: RunState) -> None:
    items = load_index()
    items = [i for i in items if i.get("run_id") != run.run_id]
    items.insert(0, {
        "run_id": run.run_id,
        "benchmark_id": run.benchmark_id,
        "status": run.status,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "summary": run.summary,
    })
    save_index(items)


# -----------------------------------------------------------------------------
# Benchmark runner
# -----------------------------------------------------------------------------


class RunRequest(BaseModel):
    benchmark_id: str
    duration_sec: int = 60
    interval_ms: int = 1000
    api_url: Optional[str] = None
    token: Optional[str] = None
    concurrency: int = 4
    limit: int = 10
    quality: str = "fast"
    stages: int = 4
    stage_duration_sec: int = 20


@dataclass
class PreflightResult:
    name: str
    ok: bool
    required: bool
    ms: Optional[float] = None
    error: Optional[str] = None


def run_preflight(config: Dict[str, Any], require_token: bool = False) -> List[PreflightResult]:
    api_url = config.get("api_url", DEFAULTS["api_url"])
    qdrant_url = config.get("qdrant_url", DEFAULTS["qdrant_url"])
    qdrant_api_key = config.get("qdrant_api_key", DEFAULTS.get("qdrant_api_key", ""))
    tei_url = config.get("tei_url", DEFAULTS["tei_url"])
    rerank_url = config.get("rerank_url", DEFAULTS["rerank_url"])
    ocr_url = config.get("ocr_url", DEFAULTS["ocr_url"])
    ocr_enabled = config.get("ocr_enabled", "false") == "true"

    checks: List[PreflightResult] = []

    def check(name: str, url: str, required: bool, headers: Optional[Dict[str, str]] = None) -> None:
        result = http_get(url, headers=headers)
        checks.append(PreflightResult(
            name=name,
            ok=result.get("ok", False),
            required=required,
            ms=result.get("ms"),
            error=result.get("error"),
        ))

    check("api_health", f"{api_url}/health", required=True)
    qdrant_headers = {"api-key": qdrant_api_key} if qdrant_api_key else None
    check("qdrant_health", f"{qdrant_url}/healthz", required=False, headers=qdrant_headers)
    check("tei_health", f"{tei_url}/health", required=False)
    check("rerank_health", f"{rerank_url}/health", required=False)
    if ocr_enabled:
        check("ocr_health", f"{ocr_url}/health", required=False)

    # Disk free check
    try:
        usage = shutil.disk_usage(str(DATA_DIR))
        free_gb = usage.free / (1024 ** 3)
        ok = free_gb > 1.0
        checks.append(PreflightResult(
            name="disk_free_gb",
            ok=ok,
            required=True,
            ms=None,
            error=None if ok else f"low disk space: {free_gb:.2f} GB",
        ))
    except Exception as e:
        checks.append(PreflightResult(
            name="disk_free_gb",
            ok=False,
            required=True,
            ms=None,
            error=str(e),
        ))

    # Token check if provided
    token = config.get("token") or load_default_token()
    if require_token and not token:
        checks.append(PreflightResult(
            name="token_valid",
            ok=False,
            required=True,
            ms=None,
            error="missing token for authenticated benchmark",
        ))
        return checks
    if token:
        headers = {"Authorization": f"Bearer {token}"}
        result = http_get(f"{api_url}/api/v1/sources", timeout=5.0, headers=headers)
        ok = result.get("ok") and result.get("status") == 200
        if not ok:
            checks.append(PreflightResult(
                name="token_valid",
                ok=False,
                required=require_token,
                ms=result.get("ms"),
                error="token invalid or missing permissions",
            ))
        else:
            checks.append(PreflightResult(
                name="token_valid",
                ok=True,
                required=require_token,
                ms=result.get("ms"),
                error=None,
            ))

    return checks


def percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    values = sorted(values)
    k = int(round((pct / 100.0) * (len(values) - 1)))
    return values[k]


def run_system_pulse(run: RunState, config: Dict[str, Any], run_dir: Path) -> None:
    sampler = SystemSampler()

    api_url = config.get("api_url", DEFAULTS["api_url"])
    token = config.get("token") or load_default_token()

    metrics_file = run_dir / "metrics.jsonl"
    logs_file = run_dir / "logs.jsonl"

    def push(event: Dict[str, Any]) -> None:
        run.queue.put(event)

    def log(msg: str, level: str = "info") -> None:
        entry = {"ts": utc_now_iso(), "level": level, "message": msg}
        append_jsonl(logs_file, entry)
        push({"type": "log", **entry})

    def update_status(status: str, error: Optional[str] = None) -> None:
        run.status = status
        if error:
            run.error = error
        persist_run_state(run, run_dir)
        update_index_for_run(run)
        push({"type": "status", "status": status, "error": error})

    # Preflight
    log("Starting preflight checks")
    preflight = run_preflight({
        "api_url": api_url,
        "qdrant_url": DEFAULTS["qdrant_url"],
        "tei_url": DEFAULTS["tei_url"],
        "rerank_url": DEFAULTS["rerank_url"],
        "ocr_url": DEFAULTS["ocr_url"],
        "ocr_enabled": DEFAULTS["ocr_enabled"],
        "token": token,
    })
    run.preflight = [p.__dict__ for p in preflight]
    persist_run_state(run, run_dir)
    push({"type": "preflight", "checks": run.preflight})

    if any((not c.ok and c.required) for c in preflight):
        log("Preflight failed", "error")
        update_status("failed", "Preflight checks failed")
        return

    run.started_at = utc_now_iso()
    persist_run_state(run, run_dir)
    update_status("running")
    log("Benchmark run started")

    duration = max(int(config.get("duration_sec", 60)), 5)
    interval = max(int(config.get("interval_ms", 1000)), 250) / 1000.0

    metrics_samples: List[Dict[str, Any]] = []
    health_ms_list: List[float] = []
    metrics_ms_list: List[float] = []
    search_ms_list: List[float] = []

    # Warmup (1 interval)
    time.sleep(interval)

    start_ts = time.time()
    tick = 0

    while True:
        if run.cancel_event.is_set():
            log("Benchmark cancelled by user", "warning")
            run.ended_at = utc_now_iso()
            run.status = "cancelled"
            persist_run_state(run, run_dir)
            update_index_for_run(run)
            push({"type": "status", "status": "cancelled"})
            return

        elapsed = time.time() - start_ts
        if elapsed >= duration:
            break

        tick += 1
        sample_ts = utc_now_iso()
        sys_stats = sampler.sample()

        health_res = http_get(f"{api_url}/health")
        metrics_res = http_get(f"{api_url}/metrics")

        health_ms = health_res.get("ms") if health_res.get("ok") else None
        metrics_ms = metrics_res.get("ms") if metrics_res.get("ok") else None

        if health_ms is not None:
            health_ms_list.append(float(health_ms))
        if metrics_ms is not None:
            metrics_ms_list.append(float(metrics_ms))

        search_ms = None
        if token:
            payload = {"query": "auth", "limit": 5, "quality": "fast"}
            headers = {"Authorization": f"Bearer {token}"}
            search_res = http_post_json(f"{api_url}/api/v1/search", payload, headers=headers, timeout=10.0)
            if search_res.get("ok"):
                search_ms = search_res.get("ms")
                search_ms_list.append(float(search_ms))

        point = {
            "ts": sample_ts,
            "tick": tick,
            "health_ms": health_ms,
            "metrics_ms": metrics_ms,
            "search_ms": search_ms,
            **sys_stats,
        }
        metrics_samples.append(point)
        append_jsonl(metrics_file, point)
        push({"type": "metric", "data": point})

        # jitter to avoid aliasing
        sleep_time = max(0.0, interval + random.uniform(-0.05, 0.05))
        time.sleep(sleep_time)

    # Summary
    run.ended_at = utc_now_iso()
    run.status = "completed"

    summary = {
        "duration_sec": duration,
        "samples": len(metrics_samples),
        "health_ms_p50": percentile(health_ms_list, 50),
        "health_ms_p95": percentile(health_ms_list, 95),
        "metrics_ms_p50": percentile(metrics_ms_list, 50),
        "metrics_ms_p95": percentile(metrics_ms_list, 95),
        "search_ms_p50": percentile(search_ms_list, 50),
        "search_ms_p95": percentile(search_ms_list, 95),
        "cpu_avg": sum([m.get("cpu_pct") or 0 for m in metrics_samples]) / max(len(metrics_samples), 1),
        "mem_avg": sum([m.get("mem_used_pct") or 0 for m in metrics_samples]) / max(len(metrics_samples), 1),
        "iowait_avg": sum([m.get("iowait_pct") or 0 for m in metrics_samples]) / max(len(metrics_samples), 1),
    }
    run.summary = summary

    persist_run_state(run, run_dir)
    update_index_for_run(run)
    push({"type": "status", "status": "completed"})
    log("Benchmark run completed")


def run_search_latency(run: RunState, config: Dict[str, Any], run_dir: Path) -> None:
    sampler = SystemSampler()

    api_url = config.get("api_url", DEFAULTS["api_url"])
    token = config.get("token") or load_default_token()
    quality = config.get("quality", "fast")
    limit = max(int(config.get("limit", 10)), 1)
    concurrency = max(int(config.get("concurrency", 4)), 1)

    metrics_file = run_dir / "metrics.jsonl"
    logs_file = run_dir / "logs.jsonl"

    def push(event: Dict[str, Any]) -> None:
        run.queue.put(event)

    def log(msg: str, level: str = "info") -> None:
        entry = {"ts": utc_now_iso(), "level": level, "message": msg}
        append_jsonl(logs_file, entry)
        push({"type": "log", **entry})

    def update_status(status: str, error: Optional[str] = None) -> None:
        run.status = status
        if error:
            run.error = error
        persist_run_state(run, run_dir)
        update_index_for_run(run)
        push({"type": "status", "status": status, "error": error})

    log("Starting preflight checks")
    preflight = run_preflight({
        "api_url": api_url,
        "qdrant_url": DEFAULTS["qdrant_url"],
        "qdrant_api_key": DEFAULTS.get("qdrant_api_key", ""),
        "tei_url": DEFAULTS["tei_url"],
        "rerank_url": DEFAULTS["rerank_url"],
        "ocr_url": DEFAULTS["ocr_url"],
        "ocr_enabled": DEFAULTS["ocr_enabled"],
        "token": token,
    }, require_token=True)
    run.preflight = [p.__dict__ for p in preflight]
    persist_run_state(run, run_dir)
    push({"type": "preflight", "checks": run.preflight})

    if any((not c.ok and c.required) for c in preflight):
        log("Preflight failed", "error")
        update_status("failed", "Preflight checks failed")
        return

    run.started_at = utc_now_iso()
    persist_run_state(run, run_dir)
    update_status("running")
    log(f"Search latency run started (quality={quality}, limit={limit}, concurrency={concurrency})")

    duration = max(int(config.get("duration_sec", 60)), 5)
    interval = max(int(config.get("interval_ms", 1000)), 250) / 1000.0

    queries = [
        "auth", "qdrant", "index", "pdf", "chunker", "vector", "search", "token",
        "api", "postgres", "rerank", "ocr"
    ]

    all_latencies: List[float] = []
    error_count = 0
    metrics_samples: List[Dict[str, Any]] = []

    headers = {"Authorization": f"Bearer {token}"} if token else {}

    # Warmup
    time.sleep(interval)

    start_ts = time.time()
    tick = 0

    from concurrent.futures import ThreadPoolExecutor, as_completed
    executor = ThreadPoolExecutor(max_workers=min(concurrency, 32))

    try:
        while True:
            if run.cancel_event.is_set():
                log("Benchmark cancelled by user", "warning")
                run.ended_at = utc_now_iso()
                run.status = "cancelled"
                persist_run_state(run, run_dir)
                update_index_for_run(run)
                push({"type": "status", "status": "cancelled"})
                return

            elapsed = time.time() - start_ts
            if elapsed >= duration:
                break

            tick += 1
            sample_ts = utc_now_iso()
            sys_stats = sampler.sample()

            latencies = []
            errors = 0
            timeouts = 0

            futures = []
            for i in range(concurrency):
                query = queries[(tick * concurrency + i) % len(queries)]
                payload = {"query": query, "limit": limit, "quality": quality}
                futures.append(executor.submit(http_post_json, f"{api_url}/api/v1/search", payload, headers, 15.0))

            # Only wait up to the interval for results to keep UI responsive
            try:
                for fut in as_completed(futures, timeout=interval):
                    result = fut.result()
                    if result.get("ok"):
                        latencies.append(float(result.get("ms", 0.0)))
                    else:
                        errors += 1
            except Exception:
                # as_completed timeout or executor hiccup
                pass

            inflight = 0
            for fut in futures:
                if not fut.done():
                    inflight += 1
                    fut.cancel()

            if inflight > 0:
                timeouts += inflight
                errors += inflight

            if latencies:
                all_latencies.extend(latencies)
            error_count += errors

            avg_latency = sum(latencies) / len(latencies) if latencies else None
            completed = len(latencies)
            reqs = completed + errors
            rps = (completed / interval) if interval > 0 else None

            point = {
                "ts": sample_ts,
                "tick": tick,
                "search_ms": avg_latency,
                "search_err": errors,
                "search_timeouts": timeouts,
                "search_inflight": inflight,
                "search_rps": rps,
                **sys_stats,
            }
            metrics_samples.append(point)
            append_jsonl(metrics_file, point)
            push({"type": "metric", "data": point})

            sleep_time = max(0.0, interval + random.uniform(-0.05, 0.05))
            time.sleep(sleep_time)
    finally:
        executor.shutdown(wait=False)

    run.ended_at = utc_now_iso()
    run.status = "completed"

    total_requests = len(all_latencies) + error_count
    duration_actual = max(time.time() - start_ts, 1e-6)
    summary = {
        "duration_sec": duration,
        "samples": len(metrics_samples),
        "search_p50": percentile(all_latencies, 50),
        "search_p95": percentile(all_latencies, 95),
        "search_p99": percentile(all_latencies, 99),
        "search_error_rate": (error_count / total_requests) if total_requests else None,
        "search_rps": (total_requests / duration_actual) if total_requests else None,
        "cpu_avg": sum([m.get("cpu_pct") or 0 for m in metrics_samples]) / max(len(metrics_samples), 1),
        "mem_avg": sum([m.get("mem_used_pct") or 0 for m in metrics_samples]) / max(len(metrics_samples), 1),
        "iowait_avg": sum([m.get("iowait_pct") or 0 for m in metrics_samples]) / max(len(metrics_samples), 1),
    }
    run.summary = summary

    persist_run_state(run, run_dir)
    update_index_for_run(run)
    push({"type": "status", "status": "completed"})
    log("Benchmark run completed")


def run_search_ramp(run: RunState, config: Dict[str, Any], run_dir: Path) -> None:
    sampler = SystemSampler()

    api_url = config.get("api_url", DEFAULTS["api_url"])
    token = config.get("token") or load_default_token()
    quality = config.get("quality", "fast")
    limit = max(int(config.get("limit", 10)), 1)
    base_concurrency = max(int(config.get("concurrency", 2)), 1)
    stages = max(int(config.get("stages", 4)), 1)
    stage_duration = max(int(config.get("stage_duration_sec", 20)), 5)

    metrics_file = run_dir / "metrics.jsonl"
    logs_file = run_dir / "logs.jsonl"

    def push(event: Dict[str, Any]) -> None:
        run.queue.put(event)

    def log(msg: str, level: str = "info") -> None:
        entry = {"ts": utc_now_iso(), "level": level, "message": msg}
        append_jsonl(logs_file, entry)
        push({"type": "log", **entry})

    def update_status(status: str, error: Optional[str] = None) -> None:
        run.status = status
        if error:
            run.error = error
        persist_run_state(run, run_dir)
        update_index_for_run(run)
        push({"type": "status", "status": status, "error": error})

    log("Starting preflight checks")
    preflight = run_preflight({
        "api_url": api_url,
        "qdrant_url": DEFAULTS["qdrant_url"],
        "qdrant_api_key": DEFAULTS.get("qdrant_api_key", ""),
        "tei_url": DEFAULTS["tei_url"],
        "rerank_url": DEFAULTS["rerank_url"],
        "ocr_url": DEFAULTS["ocr_url"],
        "ocr_enabled": DEFAULTS["ocr_enabled"],
        "token": token,
    }, require_token=True)
    run.preflight = [p.__dict__ for p in preflight]
    persist_run_state(run, run_dir)
    push({"type": "preflight", "checks": run.preflight})

    if any((not c.ok and c.required) for c in preflight):
        log("Preflight failed", "error")
        update_status("failed", "Preflight checks failed")
        return

    run.started_at = utc_now_iso()
    persist_run_state(run, run_dir)
    update_status("running")
    log(f"Search ramp started (quality={quality}, limit={limit}, base_concurrency={base_concurrency}, stages={stages})")

    interval = max(int(config.get("interval_ms", 1000)), 250) / 1000.0
    total_duration = stages * stage_duration

    queries = [
        "auth", "qdrant", "index", "pdf", "chunker", "vector", "search", "token",
        "api", "postgres", "rerank", "ocr"
    ]

    all_latencies: List[float] = []
    error_count = 0
    metrics_samples: List[Dict[str, Any]] = []
    stage_latencies: Dict[int, List[float]] = {s: [] for s in range(stages)}
    stage_errors: Dict[int, int] = {s: 0 for s in range(stages)}
    stage_counts: Dict[int, int] = {s: 0 for s in range(stages)}

    headers = {"Authorization": f"Bearer {token}"} if token else {}

    # Warmup
    time.sleep(interval)

    start_ts = time.time()
    tick = 0
    current_stage = 0
    log(f"Stage 1/{stages} started (concurrency={base_concurrency})")

    from concurrent.futures import ThreadPoolExecutor, as_completed
    executor = ThreadPoolExecutor(max_workers=min(base_concurrency * stages, 64))

    try:
        while True:
            if run.cancel_event.is_set():
                log("Benchmark cancelled by user", "warning")
                run.ended_at = utc_now_iso()
                run.status = "cancelled"
                persist_run_state(run, run_dir)
                update_index_for_run(run)
                push({"type": "status", "status": "cancelled"})
                return

            elapsed = time.time() - start_ts
            if elapsed >= total_duration:
                break

            stage_index = min(int(elapsed // stage_duration), stages - 1)
            if stage_index != current_stage:
                current_stage = stage_index
                stage_concurrency = base_concurrency * (current_stage + 1)
                log(f"Stage {current_stage + 1}/{stages} started (concurrency={stage_concurrency})")

            tick += 1
            sample_ts = utc_now_iso()
            sys_stats = sampler.sample()

            stage_concurrency = base_concurrency * (current_stage + 1)
            latencies = []
            errors = 0
            timeouts = 0

            futures = []
            for i in range(stage_concurrency):
                query = queries[(tick * stage_concurrency + i) % len(queries)]
                payload = {"query": query, "limit": limit, "quality": quality}
                futures.append(executor.submit(http_post_json, f"{api_url}/api/v1/search", payload, headers, 15.0))

            try:
                for fut in as_completed(futures, timeout=interval):
                    result = fut.result()
                    if result.get("ok"):
                        latencies.append(float(result.get("ms", 0.0)))
                    else:
                        errors += 1
            except Exception:
                pass

            inflight = 0
            for fut in futures:
                if not fut.done():
                    inflight += 1
                    fut.cancel()

            if inflight > 0:
                timeouts += inflight
                errors += inflight

            if latencies:
                all_latencies.extend(latencies)
                stage_latencies[current_stage].extend(latencies)
            error_count += errors
            stage_errors[current_stage] += errors
            stage_counts[current_stage] += len(latencies)

            avg_latency = sum(latencies) / len(latencies) if latencies else None
            completed = len(latencies)
            reqs = completed + errors
            rps = (completed / interval) if interval > 0 else None

            point = {
                "ts": sample_ts,
                "tick": tick,
                "stage": current_stage + 1,
                "concurrency": stage_concurrency,
                "search_ms": avg_latency,
                "search_err": errors,
                "search_timeouts": timeouts,
                "search_inflight": inflight,
                "search_rps": rps,
                **sys_stats,
            }
            metrics_samples.append(point)
            append_jsonl(metrics_file, point)
            push({"type": "metric", "data": point})

            sleep_time = max(0.0, interval + random.uniform(-0.05, 0.05))
            time.sleep(sleep_time)
    finally:
        executor.shutdown(wait=False)

    run.ended_at = utc_now_iso()
    run.status = "completed"

    duration_actual = max(time.time() - start_ts, 1e-6)
    stage_summary = []
    for s in range(stages):
        lat = stage_latencies[s]
        count = stage_counts[s]
        errs = stage_errors[s]
        total = count + errs
        stage_summary.append({
            "stage": s + 1,
            "concurrency": base_concurrency * (s + 1),
            "p50": percentile(lat, 50),
            "p95": percentile(lat, 95),
            "p99": percentile(lat, 99),
            "error_rate": (errs / total) if total else None,
        })

    total_requests = len(all_latencies) + error_count
    summary = {
        "duration_sec": total_duration,
        "samples": len(metrics_samples),
        "search_p50": percentile(all_latencies, 50),
        "search_p95": percentile(all_latencies, 95),
        "search_p99": percentile(all_latencies, 99),
        "search_error_rate": (error_count / total_requests) if total_requests else None,
        "search_rps": (total_requests / duration_actual) if total_requests else None,
        "stage_summary": stage_summary,
        "cpu_avg": sum([m.get("cpu_pct") or 0 for m in metrics_samples]) / max(len(metrics_samples), 1),
        "mem_avg": sum([m.get("mem_used_pct") or 0 for m in metrics_samples]) / max(len(metrics_samples), 1),
        "iowait_avg": sum([m.get("iowait_pct") or 0 for m in metrics_samples]) / max(len(metrics_samples), 1),
    }
    run.summary = summary

    persist_run_state(run, run_dir)
    update_index_for_run(run)
    push({"type": "status", "status": "completed"})
    log("Benchmark run completed")


# -----------------------------------------------------------------------------
# FastAPI app
# -----------------------------------------------------------------------------

app = FastAPI(title="MainRAG Benchmark Dashboard")

# Serve assets from /static to avoid routing conflicts with API endpoints.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/benchmarks")
async def list_benchmarks() -> JSONResponse:
    return JSONResponse(BENCHMARKS)


@app.get("/api/runs")
async def list_runs() -> JSONResponse:
    return JSONResponse(load_index())


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str, include_metrics: bool = False) -> JSONResponse:
    run_dir = DATA_DIR / run_id
    run_file = run_dir / "run.json"
    if not run_file.exists():
        raise HTTPException(status_code=404, detail="Run not found")
    run_data = json.loads(run_file.read_text())

    if include_metrics:
        metrics_file = run_dir / "metrics.jsonl"
        if metrics_file.exists():
            metrics = [json.loads(line) for line in metrics_file.read_text().splitlines() if line.strip()]
        else:
            metrics = []
        run_data["metrics"] = metrics
    return JSONResponse(run_data)


@app.post("/api/runs")
async def start_run(req: RunRequest) -> JSONResponse:
    if req.benchmark_id not in {"system_pulse", "search_latency", "search_ramp"}:
        raise HTTPException(status_code=400, detail="Unknown benchmark id")

    run_id = f"run_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    run_dir = DATA_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "duration_sec": req.duration_sec,
        "interval_ms": req.interval_ms,
        "api_url": req.api_url or DEFAULTS["api_url"],
        "token_present": bool(req.token or load_default_token()),
        "benchmark_id": req.benchmark_id,
        "concurrency": req.concurrency,
        "quality": req.quality,
        "limit": req.limit,
        "stages": req.stages,
        "stage_duration_sec": req.stage_duration_sec,
    }

    run = RunState(
        run_id=run_id,
        benchmark_id=req.benchmark_id,
        status="queued",
        created_at=utc_now_iso(),
        config=config,
    )

    with ACTIVE_LOCK:
        ACTIVE_RUNS[run_id] = run

    persist_run_state(run, run_dir)
    update_index_for_run(run)

    # Start benchmark in background
    runner_args = {
        "duration_sec": req.duration_sec,
        "interval_ms": req.interval_ms,
        "api_url": req.api_url or DEFAULTS["api_url"],
        "token": req.token or load_default_token(),
        "concurrency": req.concurrency,
        "quality": req.quality,
        "limit": req.limit,
        "stages": req.stages,
        "stage_duration_sec": req.stage_duration_sec,
    }
    if req.benchmark_id == "search_latency":
        target = run_search_latency
    elif req.benchmark_id == "search_ramp":
        target = run_search_ramp
    else:
        target = run_system_pulse
    thread = threading.Thread(
        target=target,
        args=(run, runner_args, run_dir),
        daemon=True,
    )
    thread.start()

    return JSONResponse({"run_id": run_id})


@app.post("/api/runs/{run_id}/stop")
async def stop_run(run_id: str) -> JSONResponse:
    with ACTIVE_LOCK:
        run = ACTIVE_RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    run.cancel_event.set()
    return JSONResponse({"status": "stopping"})


@app.get("/api/runs/{run_id}/events")
async def stream_events(run_id: str) -> StreamingResponse:
    with ACTIVE_LOCK:
        run = ACTIVE_RUNS.get(run_id)
    if not run:
        # allow SSE for completed runs by reading stored metrics?
        raise HTTPException(status_code=404, detail="Run not found")

    async def event_generator() -> Any:
        # Send initial status
        yield f"data: {json.dumps({'type': 'status', 'status': run.status})}\n\n"
        while True:
            try:
                event = run.queue.get(timeout=1)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") == "status" and event.get("status") in ("completed", "failed", "cancelled"):
                    break
            except queue.Empty:
                # keep-alive
                yield "data: {}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/config")
async def get_config() -> JSONResponse:
    return JSONResponse(DEFAULTS)


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})

@app.get("/app.js")
async def legacy_app_js() -> FileResponse:
    return FileResponse(STATIC_DIR / "app.js")

@app.get("/favicon.ico")
async def favicon() -> JSONResponse:
    return JSONResponse(status_code=204, content=None)


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8787,
        reload=False,
    )
