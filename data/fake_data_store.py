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
from datetime import datetime, timedelta

from incident_schema import Alert, DeployEvent, LogEntry, MetricPoint, TraceSpan

random.seed(7)  # deterministic demo data

SERVICE = "checkout-api"
#bad deploy means that a deployment was bad or which caused the error
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
            deployed_at=datetime.utcnow() - timedelta(minutes=12),
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
            deployed_at=datetime.utcnow() - timedelta(hours=3),
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
            deployed_at=datetime.utcnow() - timedelta(minutes=25),
        ),
        "log_message": "TimeoutError: could not acquire connection from pool (max=5)",
    },
}

#builds an alert about what has happened
def build_alert(scenario_key: str) -> Alert:
    s = SCENARIOS[scenario_key]
    return Alert(
        service=SERVICE,
        alert_type=s["alert_type"],
        metric_name=s["metric_name"],
        threshold_breached=s["threshold_breached"],
        current_value=s["current_value"],
        triggered_at=datetime.utcnow(),
    )

#generates 6 fake querry logs for testing
def query_logs(scenario_key: str, minutes: int = 30) -> list[LogEntry]:
    s = SCENARIOS[scenario_key]
    now = datetime.utcnow()
    logs = []
    for i in range(6):
        logs.append(
            LogEntry(
                timestamp=now - timedelta(minutes=random.randint(0, minutes)),
                level="ERROR" if i % 2 == 0 else "WARN",
                service=SERVICE,
                message=s["log_message"] if i % 2 == 0 else "Elevated response time observed",
            )
        )
    return sorted(logs, key=lambda l: l.timestamp)


def query_metrics(scenario_key: str, minutes: int = 30) -> list[MetricPoint]:
    s = SCENARIOS[scenario_key]
    now = datetime.utcnow()
    points = []
    for i in range(minutes, 0, -5):
        # ramp up toward the current breached value
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
    now = datetime.utcnow()
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
        deployed_at=datetime.utcnow() - timedelta(days=1),
    )
    return [s["bad_commit"], older]

#This module is a fake data generator that mimics calling real
# observability tools. Every function signature (build_alert,
# query_logs, query_metrics, query_traces, get_deploy_history)
# is designed to look like what a real integration would expose,
# so that later, someone can replace the implementation 
#(swap fake generation for real Datadog/Prometheus/Jaeger/GitHub
# API calls) without touching the Investigator agent that 
#consumes these functions. It's a classic
# "fake it till you make it" backend used to develop and test 
#an end-to-end pipeline before the real integrations exist —
# and the random.seed(7) ensures the fake data is reproducible for
# testing/demo purposes.
