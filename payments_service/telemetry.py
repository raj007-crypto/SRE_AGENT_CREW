"""
Telemetry for the payments service: the three pillars an SRE (or an agent)
investigates with.

  * Logs     -- structured JSON, to stdout, optionally to a rotating file, and
                to an in-memory ring buffer served by GET /admin/logs.
  * Metrics  -- real Prometheus metrics at GET /metrics, plus a windowed JSON
                summary at GET /stats (error rate, p50/p95/p99, per endpoint).
  * Traces   -- one lightweight trace per request, made of timed spans
                (validate -> db.query -> bank.charge), served by GET /admin/traces.

Everything is held per-app-instance (no globals), so tests can create many
independent services without metric-registry collisions.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import time
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Callable, Iterator, Optional

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from .settings import Settings

SERVICE_NAME = "payments-api"
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


def _rss_bytes() -> Optional[int]:
    """Current resident memory of this process (Linux only; None elsewhere)."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(round(pct / 100 * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return record.getMessage()  # the message is already a JSON string


class RequestTrace:
    """One request's spans. Created per request, stored when the request ends."""

    def __init__(self, endpoint: str) -> None:
        self.request_id = "req_" + uuid.uuid4().hex[:10]
        self.endpoint = endpoint
        self.started = time.time()
        self.spans: list[dict] = []

    @contextmanager
    def span(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        error = False
        try:
            yield
        except BaseException:
            error = True
            raise
        finally:
            self.spans.append(
                {
                    "timestamp": time.time(),
                    "operation": name,
                    "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                    "error": error,
                }
            )

    def to_dict(self, status: int, total_ms: float) -> dict:
        return {
            "request_id": self.request_id,
            "endpoint": self.endpoint,
            "started": self.started,
            "status": status,
            "duration_ms": round(total_ms, 2),
            "error": status >= 500,
            "spans": self.spans,
        }


class Telemetry:
    def __init__(self, settings: Settings, version_getter: Callable[[], str]) -> None:
        self.settings = settings
        self._version = version_getter

        self.logs: deque[dict] = deque(maxlen=settings.max_logs)
        self.requests: deque[tuple[float, str, int, float]] = deque(maxlen=settings.max_requests)
        self.traces: deque[dict] = deque(maxlen=settings.max_traces)

        # ---- logging plumbing (unique logger per instance => no duplicate handlers)
        self._logger = logging.getLogger(f"{SERVICE_NAME}.{uuid.uuid4().hex[:8]}")
        self._logger.setLevel(logging.DEBUG)
        self._logger.propagate = False
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(_JsonFormatter())
        self._logger.addHandler(stream)
        if settings.log_file:
            fh = logging.handlers.RotatingFileHandler(
                settings.log_file, maxBytes=5_000_000, backupCount=2
            )
            fh.setFormatter(_JsonFormatter())
            self._logger.addHandler(fh)
        self._quiet = False

        # ---- prometheus
        self.registry = CollectorRegistry()
        self.m_requests = Counter(
            "payments_http_requests_total", "HTTP requests",
            ["endpoint", "status"], registry=self.registry,
        )
        self.m_duration = Histogram(
            "payments_http_request_duration_seconds", "Request latency",
            ["endpoint"], registry=self.registry,
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
        )
        self.g_pool_size = Gauge("payments_db_pool_size", "DB pool size", registry=self.registry)
        self.g_pool_in_use = Gauge("payments_db_pool_in_use", "DB connections in use", registry=self.registry)
        self.g_leak_bytes = Gauge("payments_cache_retained_bytes", "Bytes held by the merchant cache", registry=self.registry)
        self.g_rss = Gauge("payments_process_resident_memory_bytes", "Process RSS", registry=self.registry)
        self.g_build = Gauge("payments_build_info", "Running build", ["version", "commit"], registry=self.registry)
        self._build_label: Optional[tuple[str, str]] = None

    # ------------------------------------------------------------------ logs
    def silence_stdout(self) -> None:
        """Used by tests to keep pytest output clean (ring buffer still fills)."""
        self._quiet = True
        for h in list(self._logger.handlers):
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                self._logger.removeHandler(h)

    def log(self, level: str, message: str, **fields) -> dict:
        now = time.time()
        entry = {
            "timestamp": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "ts": now,
            "level": level,
            "service": SERVICE_NAME,
            "version": self._version(),
            "message": message,
            **{k: v for k, v in fields.items() if v is not None},
        }
        self.logs.append(entry)
        self._logger.log(LEVELS.get(level, 20), json.dumps(entry, default=str))
        return entry

    def query_logs(
        self,
        since: Optional[float] = None,
        until: Optional[float] = None,
        min_level: str = "DEBUG",
        limit: int = 500,
        contains: Optional[str] = None,
    ) -> list[dict]:
        floor = LEVELS.get(min_level.upper(), 10)
        out = []
        for e in self.logs:
            if since is not None and e["ts"] < since:
                continue
            if until is not None and e["ts"] > until:
                continue
            if LEVELS.get(e["level"], 20) < floor:
                continue
            if contains and contains.lower() not in json.dumps(e, default=str).lower():
                continue
            out.append(e)
        return out[-limit:]

    # ---------------------------------------------------------------- traces
    def new_trace(self, endpoint: str) -> RequestTrace:
        return RequestTrace(endpoint)

    def query_traces(self, since: Optional[float] = None, errors_only: bool = False, limit: int = 100) -> list[dict]:
        out = [
            t for t in self.traces
            if (since is None or t["started"] >= since) and (not errors_only or t["error"])
        ]
        return out[-limit:]

    # --------------------------------------------------------------- metrics
    def record_request(self, trace: RequestTrace, status: int, latency_s: float) -> None:
        self.requests.append((time.time(), trace.endpoint, status, latency_s * 1000))
        self.traces.append(trace.to_dict(status, latency_s * 1000))
        self.m_requests.labels(endpoint=trace.endpoint, status=str(status)).inc()
        self.m_duration.labels(endpoint=trace.endpoint).observe(latency_s)

    def render_metrics(self, *, pool_size: int, pool_in_use: int, leak_bytes: int,
                       version: str, commit: str) -> bytes:
        self.g_pool_size.set(pool_size)
        self.g_pool_in_use.set(pool_in_use)
        self.g_leak_bytes.set(leak_bytes)
        rss = _rss_bytes()
        if rss is not None:
            self.g_rss.set(rss)
        label = (version, commit)
        if label != self._build_label:
            if self._build_label:
                self.g_build.remove(*self._build_label)
            self.g_build.labels(version=version, commit=commit).set(1)
            self._build_label = label
        return generate_latest(self.registry)

    def stats(self, window: float = 60.0, now: Optional[float] = None) -> dict:
        """Windowed summary. Only 5xx count as errors (a card decline is not an outage)."""
        now = now or time.time()
        cutoff = now - window
        rows = [r for r in self.requests if r[0] >= cutoff]

        def summarize(subset: list[tuple[float, str, int, float]]) -> dict:
            total = len(subset)
            errors = sum(1 for r in subset if r[2] >= 500)
            lats = sorted(r[3] for r in subset)
            return {
                "requests": total,
                "errors_5xx": errors,
                "error_rate": round(errors / total, 4) if total else 0.0,
                "rps": round(total / window, 2),
                "latency_ms": {
                    "p50": round(_percentile(lats, 50), 1),
                    "p95": round(_percentile(lats, 95), 1),
                    "p99": round(_percentile(lats, 99), 1),
                    "max": round(lats[-1], 1) if lats else 0.0,
                },
            }

        by_endpoint = {}
        for ep in {r[1] for r in rows}:
            by_endpoint[ep] = summarize([r for r in rows if r[1] == ep])

        status_counts: dict[str, int] = {}
        for r in rows:
            status_counts[str(r[2])] = status_counts.get(str(r[2]), 0) + 1

        return {
            "window_seconds": window,
            "overall": summarize(rows),
            "by_endpoint": by_endpoint,
            "status_counts": status_counts,
        }
