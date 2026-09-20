"""
PaymentsService: wires releases, faults, the connection pool and telemetry
together and processes requests.
"""

from __future__ import annotations

import asyncio
import os
import time
import traceback
from typing import Optional

from . import handlers
from .deploys import (
    BUG_MEMORY_LEAK,
    BUG_SLOW_QUERY,
    EXTERNAL_FAULTS,
    FAULT_DOWNSTREAM_OUTAGE,
    HEALTHY_CONFIG,
    SCENARIOS,
    Deploy,
    ReleaseManager,
)
from .handlers import (
    BankUnavailable,
    CardDeclined,
    DbPool,
    PoolTimeout,
    TransientUpstream,
    ValidationFailed,
)
from .settings import Settings
from .telemetry import Telemetry, _rss_bytes

_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DECLINE_REASONS = ["insufficient_funds", "card_expired", "do_not_honor"]


def _short_stack(exc: BaseException, depth: int = 3) -> list[str]:
    """A compact, path-relative stack like a real log line would carry."""
    frames = traceback.extract_tb(exc.__traceback__)[-depth:]
    return [
        f"{os.path.relpath(f.filename, _PKG_PARENT)}:{f.lineno} in {f.name}  ->  {f.line}"
        for f in frames
    ]


class PaymentsService:
    def __init__(self, settings: Settings) -> None:
        import random

        self.settings = settings
        self.rng = random.Random(settings.seed)
        self.started_at = time.time()
        self.releases = ReleaseManager()
        self.external_faults: set[str] = set()
        self.leak_cache: list[bytearray] = []
        self._leak_requests = 0
        self.telemetry = Telemetry(settings, lambda: self.releases.current.version)
        self.pool = self._new_pool()

    # ------------------------------------------------------------- helpers
    def _new_pool(self) -> DbPool:
        size = int(self.releases.current.config.get("db_pool_size", HEALTHY_CONFIG["db_pool_size"]))
        return DbPool(size, acquire_timeout=max(0.4 * self.settings.latency_scale, 0.05))

    async def _sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds * self.settings.latency_scale)

    def _restart(self, reason: str) -> None:
        """A deploy/rollback restarts the 'process': memory freed, pool rebuilt."""
        self.leak_cache = []
        self._leak_requests = 0
        self.pool = self._new_pool()
        cur = self.releases.current
        self.telemetry.log(
            "INFO", f"service restarted ({reason})",
            commit=cur.commit_sha, db_pool_size=self.pool.size,
        )

    # ------------------------------------------------------- request path
    async def process(self, endpoint: str, payload: dict) -> tuple[int, dict]:
        trace = self.telemetry.new_trace(endpoint)
        start = time.perf_counter()
        status, body = 200, {}
        error: Optional[BaseException] = None
        try:
            if endpoint == "/pay":
                body = await self._pay(payload, trace)
            elif endpoint == "/refund":
                body = await self._refund(payload, trace)
            else:  # pragma: no cover
                raise ValidationFailed("unknown endpoint")
        except (ValidationFailed, CardDeclined, PoolTimeout, BankUnavailable, TransientUpstream) as e:
            status, error = e.status, e
            body = {"error": type(e).__name__, "message": str(e)}
        except Exception as e:  # a genuine, unhandled bug -> 500 with a real traceback
            status, error = 500, e
            body = {"error": "internal_error", "message": "internal server error"}

        elapsed = time.perf_counter() - start
        self.telemetry.record_request(trace, status, elapsed)

        level = "ERROR" if status >= 500 else "WARNING" if status >= 400 else "INFO"
        fields: dict = {}
        if error is not None:
            fields["error_type"] = type(error).__name__
            fields["error_message"] = str(error)
            if status >= 500 and not isinstance(error, (PoolTimeout, BankUnavailable, TransientUpstream)):
                fields["stack"] = _short_stack(error)
        self.telemetry.log(
            level, f"POST {endpoint} -> {status}",
            request_id=trace.request_id, endpoint=endpoint, status=status,
            latency_ms=round(elapsed * 1000, 1), **fields,
        )
        return status, body

    async def _db_hold(self, span_name: str, trace, extra_seconds: float = 0.0) -> None:
        with trace.span(span_name):
            await self.pool.acquire()
            pool = self.pool  # release on the pool we actually acquired from
            try:
                await self._sleep(self.rng.uniform(0.06, 0.14) + extra_seconds)
            finally:
                pool.release()

    async def _bank_call(self, trace, label: str) -> None:
        with trace.span(label):
            if FAULT_DOWNSTREAM_OUTAGE in self.external_faults and self.rng.random() < 0.9:
                await self._sleep(self.rng.uniform(0.15, 0.30))  # waiting on a dead upstream
                raise BankUnavailable("bank-gateway returned 503 Service Unavailable")
            await self._sleep(self.rng.uniform(0.02, 0.06))
            if self.rng.random() < self.settings.baseline_error_rate:
                raise TransientUpstream("connection reset by peer (bank-gateway)")

    async def _pay(self, payload: dict, trace) -> dict:
        bug = self.releases.current.bug
        with trace.span("validate"):
            handlers.validate_payment(payload, bug)

        if bug == BUG_MEMORY_LEAK:
            await self._leaky_cache_lookup(trace)

        await self._db_hold("db.query", trace)

        if self.rng.random() < self.settings.card_decline_rate:
            raise CardDeclined(self.rng.choice(_DECLINE_REASONS))

        await self._bank_call(trace, "bank.charge")
        return {"status": "approved", "transaction_id": f"txn_{self.rng.getrandbits(40):010x}"}

    async def _refund(self, payload: dict, trace) -> dict:
        bug = self.releases.current.bug
        with trace.span("validate"):
            handlers.validate_refund(payload)

        extra = 0.0
        if bug == BUG_SLOW_QUERY:
            extra = self.rng.uniform(1.5, 2.5)  # sequential scan, no index
        await self._db_hold("db.refund_lookup", trace, extra_seconds=extra)
        if extra:
            self.telemetry.log(
                "WARNING",
                f"slow query: SELECT * FROM refunds WHERE customer_email = ? "
                f"took {int((0.1 + extra) * 1000)}ms (seq scan on refunds)",
                request_id=trace.request_id, endpoint="/refund",
            )

        await self._bank_call(trace, "bank.refund")
        return {"status": "refunded", "transaction_id": payload["transaction_id"]}

    async def _leaky_cache_lookup(self, trace) -> None:
        """The buggy merchant cache: retains memory forever, and GC pressure grows."""
        with trace.span("merchant_cache.lookup"):
            s = self.settings
            if len(self.leak_cache) < s.leak_cap_entries:
                self.leak_cache.append(bytearray(b"\x01") * (s.leak_entry_kb * 1024))
            added_ms = min(len(self.leak_cache) * s.leak_ms_per_entry, s.leak_max_added_ms)
            await self._sleep(added_ms / 1000)
            self._leak_requests += 1
            if added_ms >= 250 and self._leak_requests % 25 == 0:
                self.telemetry.log(
                    "WARNING",
                    f"GC pause {int(added_ms)}ms; heap retained by merchant_cache "
                    f"{self._cache_bytes() // (1024 * 1024)}MB and growing",
                )

    def _cache_bytes(self) -> int:
        return len(self.leak_cache) * self.settings.leak_entry_kb * 1024

    # ------------------------------------------------------------- admin ops
    def deploy(self, **kwargs) -> Deploy:
        d = self.releases.deploy(**kwargs)
        self.telemetry.log(
            "INFO", f"deploy {d.version} ({d.commit_sha}) by {d.author}: {d.message}",
            commit=d.commit_sha, kind=d.kind,
        )
        self._restart(f"deploy {d.version}")
        return d

    def rollback(self) -> tuple[Deploy, Deploy]:
        bad, now_live = self.releases.rollback()
        self.telemetry.log(
            "INFO", f"rollback {bad.version} ({bad.commit_sha}) -> {now_live.version} ({now_live.commit_sha})",
            commit=now_live.commit_sha, kind="rollback",
        )
        self._restart(f"rollback to {now_live.version}")
        return bad, now_live

    def set_fault(self, name: str, enabled: bool) -> None:
        if name not in EXTERNAL_FAULTS:
            raise KeyError(name)
        (self.external_faults.add if enabled else self.external_faults.discard)(name)
        # NB: deliberately no log line. A dead upstream doesn't announce itself in *our* logs.

    def inject(self, scenario: str) -> dict:
        spec = SCENARIOS[scenario]  # KeyError -> 404 upstream
        result: dict = {"scenario": scenario, "description": spec["description"]}
        if spec.get("deploy"):
            d = self.deploy(**spec["deploy"])
            result["deployed"] = d.public()
        if spec.get("fault"):
            self.set_fault(spec["fault"], True)
            result["fault"] = spec["fault"]
        return result

    def reset(self) -> None:
        self.external_faults.clear()
        self.releases.reset()
        self._restart("reset to healthy baseline")

    # ------------------------------------------------------------- read ops
    def state(self) -> dict:
        cur = self.releases.current
        return {
            "service": "payments-api",
            "version": cur.version,
            "commit": cur.commit_sha,
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "db_pool": {"size": self.pool.size, "in_use": self.pool.in_use},
            "cache_retained_mb": round(self._cache_bytes() / (1024 * 1024), 1),
            "process_rss_mb": round(_rss_bytes() / (1024 * 1024), 1) if _rss_bytes() else None,
        }

    def ground_truth(self) -> dict:
        """FOR TESTS AND THE DEMO DASHBOARD ONLY. Agents must never read this."""
        cur = self.releases.current
        healthy = (
            cur.bug is None
            and cur.config == HEALTHY_CONFIG
            and not self.external_faults
        )
        return {
            "healthy": healthy,
            "active_bug": cur.bug,
            "config": cur.config,
            "external_faults": sorted(self.external_faults),
            "culprit_commit": cur.commit_sha if (cur.bug or cur.config != HEALTHY_CONFIG) else None,
        }
