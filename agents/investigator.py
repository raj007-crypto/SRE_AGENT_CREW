"""
Investigator agent.

Job: gather evidence from every relevant system AT THE SAME TIME instead
of one after another. This is the fan-out/fan-in pattern -- three
independent tool calls launched concurrently, then merged into one
Evidence bundle.

Each query function has an artificial delay added so you can actually
see and demo the time savings vs. running them sequentially.
"""

from __future__ import annotations

import asyncio
import time

from data.fake_data_store import query_logs, query_metrics, query_traces
from incident_schema import Evidence, Incident, IncidentStatus
from utils.retry import with_retries

ARTIFICIAL_LATENCY_SECONDS = 1.0  # simulates a real API round-trip


@with_retries()
async def _query_logs_async(scenario_key: str) -> list:
    await asyncio.sleep(ARTIFICIAL_LATENCY_SECONDS)
    return query_logs(scenario_key)


@with_retries()
async def _query_metrics_async(scenario_key: str) -> list:
    await asyncio.sleep(ARTIFICIAL_LATENCY_SECONDS)
    return query_metrics(scenario_key)


@with_retries()
async def _query_traces_async(scenario_key: str) -> list:
    await asyncio.sleep(ARTIFICIAL_LATENCY_SECONDS)
    return query_traces(scenario_key)


async def run_investigator(incident: Incident, scenario_key: str) -> Incident:
    """
    Entry point for stage 2. Fans out three tool calls concurrently,
    merges results into Evidence, and writes it back onto the incident.
    """
    start = time.perf_counter()

    logs, metrics, traces = await asyncio.gather(
        _query_logs_async(scenario_key),
        _query_metrics_async(scenario_key),
        _query_traces_async(scenario_key),
    )

    elapsed = time.perf_counter() - start

    incident.evidence = Evidence(logs=logs, metrics=metrics, traces=traces)
    incident.status = IncidentStatus.INVESTIGATING

    print(f"[investigator] incident {incident.id} | gathered "
          f"{len(logs)} logs, {len(metrics)} metric points, {len(traces)} trace spans "
          f"in {elapsed:.2f}s (would be ~{ARTIFICIAL_LATENCY_SECONDS * 3:.0f}s sequential)")

    return incident