"""
Run the pipeline against a simulated incident scenario.

Usage:
    python main.py --scenario bad_deploy
    python main.py --scenario memory_leak
    python main.py --scenario bad_config
    python main.py --scenario bad_deploy --auto-approve   # skip the y/n prompt
    python main.py --scenario bad_deploy --chaos          # inject one transient
                                                            # failure per observability
                                                            # call, to demo the retry logic

The graph pauses at the approval gate (stage 4) and waits for a human
decision before continuing -- this script plays the role of "the human"
via a terminal prompt so you can demo the full pause/resume flow without
needing Slack set up yet.

Day 6: wraps both graph invocations in a try/except so a genuine,
non-retryable failure (e.g. all 3 retry attempts exhausted, or a bug)
doesn't crash with a raw traceback -- it's caught, logged, and reported
as a failed incident instead (the Incident is marked status=FAILED and
the error message is recorded on it before it's printed).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import traceback
from datetime import datetime, timezone
from uuid import uuid4

from dotenv import load_dotenv
from langgraph.types import Command

import data.fake_data_store as fake_data_store
from data.fake_data_store import SCENARIOS
from graph import build_graph, get_checkpointer
from incident_schema import Alert, Incident, IncidentStatus

load_dotenv()


def ask_for_approval(interrupt_payload: dict) -> dict:
    proposal = interrupt_payload["proposal"]
    hypothesis = interrupt_payload.get("hypothesis")

    print("\n--- APPROVAL NEEDED ---")
    if hypothesis:
        print(f"Root cause: {hypothesis['probable_cause']} "
              f"(confidence: {hypothesis['confidence']:.0%})")
    print(f"Proposed action: {proposal['action']} -> {proposal['target']}")
    print(f"Justification: {proposal['justification']}")

    answer = input("Approve this action? [y/N]: ").strip().lower()
    approved = answer == "y"

    return {
        "approved": approved,
        "approved_by": "cli-operator",
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "note": None if approved else "denied via CLI demo prompt",
    }


def _partial_incident(app, config) -> Incident | None:
    """Pull whatever Incident the checkpointer has, even after a failed run."""
    try:
        return app.get_state(config).values.get("incident")
    except Exception:
        return None


def _new_failed_incident(run_id: str, scenario_key: str) -> Incident:
    """A minimal record when the pipeline failed before an incident was opened."""
    return Incident(
        id=run_id,
        status=IncidentStatus.FAILED,
        alert=Alert(
            service=scenario_key,
            alert_type="pipeline_failure",
            metric_name="unknown",
            threshold_breached=0.0,
            current_value=0.0,
            triggered_at=datetime.now(timezone.utc),
        ),
        error="pipeline failed before an incident was opened",
    )


def _mark_failed(app, config, run_id: str, scenario_key: str, error: Exception) -> Incident:
    """Record a pipeline failure on the incident: set error + FAILED status."""
    incident = _partial_incident(app, config)
    if incident is None:
        incident = _new_failed_incident(run_id, scenario_key)
    incident.error = str(error)
    incident.status = IncidentStatus.FAILED
    return incident


async def run(scenario_key: str, auto_approve: bool, chaos: bool) -> None:
    if scenario_key not in SCENARIOS:
        raise SystemExit(f"unknown scenario '{scenario_key}'. choices: {list(SCENARIOS)}")

    fake_data_store.CHAOS_MODE = chaos
    if chaos:
        print("[main] chaos mode ON -- each observability query will fail once "
              "before succeeding on retry")

    print(f"\n=== simulating incident: {SCENARIOS[scenario_key]['description']} ===\n")

    run_id = str(uuid4())[:8]
    app = build_graph(checkpointer=get_checkpointer())
    config = {"configurable": {"thread_id": run_id}}

    incident = None
    try:
        result = await app.ainvoke(
            {"scenario_key": scenario_key, "run_id": run_id, "incident": None},
            config=config,
        )

        if "__interrupt__" in result:
            interrupt_payload = result["__interrupt__"][0].value

            if auto_approve:
                decision = {
                    "approved": True,
                    "approved_by": "auto-approve-flag",
                    "decided_at": datetime.now(timezone.utc).isoformat(),
                    "note": None,
                }
                print("\n--- AUTO-APPROVING (--auto-approve flag set) ---")
            else:
                decision = ask_for_approval(interrupt_payload)

            result = await app.ainvoke(Command(resume=decision), config=config)

        incident = result["incident"]

    except Exception as e:
        # A genuine, non-retryable failure (all retries exhausted, a bug, etc).
        # Record it on the Incident (status=FAILED + error message) so the run
        # produces a visible artifact, then report it cleanly instead of
        # dumping a raw stack trace on whoever's watching the demo.
        incident = _mark_failed(app, config, run_id, scenario_key, e)

        print("\n=== PIPELINE FAILED ===")
        print(f"run_id={run_id} scenario={scenario_key}")
        print(f"error: {e}")
        print("\nFull traceback (for debugging):")
        traceback.print_exc()

    print("\n=== final incident state ===")
    print(json.dumps(incident.model_dump(mode="json"), indent=2, default=str))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="bad_deploy", choices=list(SCENARIOS.keys()))
    parser.add_argument("--auto-approve", action="store_true",
                         help="skip the interactive y/n prompt and approve automatically")
    parser.add_argument("--chaos", action="store_true",
                         help="inject one transient failure per observability call to demo retries")
    args = parser.parse_args()

    asyncio.run(run(args.scenario, args.auto_approve, args.chaos))
