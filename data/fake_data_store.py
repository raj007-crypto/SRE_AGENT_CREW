"""
Simulated observability backend.

In production these three functions would call Datadog/Prometheus/Jaeger
APIs. For the 1-week build, they generate realistic synthetic data for a
handful of canned incident scenarios so the whole pipeline can run
end-to-end without needing real infrastructure.

Swap these out for real API clients later -- the function signatures
are what the Investigator agent calls, so nothing upstream needs to change.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from incident_schema import Alert, DeployEvent, LogEntry, MetricPoint, TraceSpan
from utils.retry import TransientAPIError

random.seed(7)  # deterministic demo data

# When True, each query function fails with a TransientAPIError on its
# first call (per process) to simulate a flaky real observability API.
# Toggled by `python main.py --chaos` so you can actually see the retry
# logic in agents/investigator.py do something, instead of it being
# dead code that only matters "in theory".
CHAOS_MODE = False
_chaos_fired: set[str] = set()


def _maybe_fail(source: str) -> None:
    if CHAOS_MODE and source not in _chaos_fired:
        _chaos_fired.add(source)
        raise TransientAPIError(f"simulated transient failure calling {source} (chaos mode)")

SERVICE = "checkout-api"

SCENARIOS = {
    "bad_deploy": {
        "description": "A bad deploy introduces a null-pointer bug in payment validation.",
        "alert_type": "error_rate_spike",
        "metric_name": "http_5xx_rate",
        "threshold_breached": 1.0,
        "current_value": 23.5,
        "bad_commit": DeployEvent(
            commit_sha="a1b2c3d",
            author="jsmith",
            message="Refactor payment validation logic",
            deployed_at=datetime.now(timezone.utc) - timedelta(minutes=12),
        ),
        "log_message": "NullPointerException in PaymentValidator.validate() at line 88",
    },
    "memory_leak": {
        "description": "A recent change holds references in a cache that's never evicted.",
        "alert_type": "latency_spike",
        "metric_name": "p99_latency_ms",
        "threshold_breached": 500.0,
        "current_value": 4200.0,
        "bad_commit": DeployEvent(
            commit_sha="e4f5g6h",
            author="rpatel",
            message="Add response caching layer for product lookups",
            deployed_at=datetime.now(timezone.utc) - timedelta(hours=3),
        ),
        "log_message": "GC pause exceeded 2000ms, heap usage at 94%",
    },
    "bad_config": {
        "description": "A config push drops the DB connection pool size too low.",
        "alert_type": "timeout_spike",
        "metric_name": "db_timeout_rate",
        "threshold_breached": 0.5,
        "current_value": 18.0,
        "bad_commit": DeployEvent(
            commit_sha="i7j8k9l",
            author="tchen",
            message="Tune connection pool settings for cost savings",
            deployed_at=datetime.now(timezone.utc) - timedelta(minutes=25),
        ),
        "log_message": "TimeoutError: could not acquire connection from pool (max=5)",
    },
}


def build_alert(scenario_key: str) -> Alert:
    s = SCENARIOS[scenario_key]
    return Alert(
        service=SERVICE,
        alert_type=s["alert_type"],
        metric_name=s["metric_name"],
        threshold_breached=s["threshold_breached"],
        current_value=s["current_value"],
        triggered_at=datetime.now(timezone.utc),
    )


def query_logs(scenario_key: str, minutes: int = 30) -> list[LogEntry]:
    _maybe_fail("logs_api")
    s = SCENARIOS[scenario_key]
    now = datetime.now(timezone.utc)
    bad_deploy = s.get("bad_commit")
    logs = []
    for i in range(6):
        if i % 2 == 0 and bad_deploy is not None:
            # ERROR logs are a *consequence* of the bad deploy -- never
            # timestamp them before the commit that caused them.
            elapsed = (now - bad_deploy.deployed_at).total_seconds()
            ts = bad_deploy.deployed_at + timedelta(seconds=random.uniform(1, max(2, elapsed)))
        else:
            ts = now - timedelta(minutes=random.randint(0, minutes))
        logs.append(
            LogEntry(
                timestamp=ts,
                level="ERROR" if i % 2 == 0 else "WARN",
                service=SERVICE,
                message=s["log_message"] if i % 2 == 0 else "Elevated response time observed",
            )
        )
    return sorted(logs, key=lambda l: l.timestamp)


def query_metrics(scenario_key: str, minutes: int = 30) -> list[MetricPoint]:
    _maybe_fail("metrics_api")
    s = SCENARIOS[scenario_key]
    now = datetime.now(timezone.utc)
    points = []
    for i in range(minutes, -1, -5):
        # ramp up toward the current breached value; the i=0 step lands
        # exactly on current_value so the trend matches what the alert fired on
        progress = (minutes - i) / minutes
        value = s["threshold_breached"] + progress * (s["current_value"] - s["threshold_breached"])
        points.append(
            MetricPoint(
                timestamp=now - timedelta(minutes=i),
                metric_name=s["metric_name"],
                value=round(value, 2),
            )
        )
    return points


def query_traces(scenario_key: str, minutes: int = 30) -> list[TraceSpan]:
    _maybe_fail("traces_api")
    now = datetime.now(timezone.utc)
    spans = []
    for i in range(5):
        spans.append(
            TraceSpan(
                timestamp=now - timedelta(minutes=random.randint(0, minutes)),
                service=SERVICE,
                operation=random.choice(["POST /checkout", "GET /cart", "POST /payment"]),
                duration_ms=round(random.uniform(200, 5000), 1),
                error=random.random() < 0.6,
            )
        )
    return spans


def get_deploy_history(scenario_key: str) -> list[DeployEvent]:
    s = SCENARIOS[scenario_key]
    older = DeployEvent(
        commit_sha="z9y8x7w",
        author="mkumar",
        message="Update logging format",
        deployed_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    return [s["bad_commit"], older]