"""
Run the pipeline against a simulated incident scenario.

Usage:
    python main.py --scenario bad_deploy
    python main.py --scenario memory_leak
    python main.py --scenario bad_config
    python main.py --scenario bad_deploy --auto-approve
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from uuid import uuid4

from langgraph.types import Command

from data.fake_data_store import SCENARIOS
from graph import build_graph, get_checkpointer


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
        "decided_at": datetime.utcnow().isoformat(),
        "note": None if approved else "denied via CLI demo prompt",
    }


async def run(scenario_key: str, auto_approve: bool) -> None:
    if scenario_key not in SCENARIOS:
        raise SystemExit(f"unknown scenario '{scenario_key}'. choices: {list(SCENARIOS)}")

    print(f"\n=== simulating incident: {SCENARIOS[scenario_key]['description']} ===\n")

    run_id = str(uuid4())[:8]
    app = build_graph(checkpointer=get_checkpointer())
    config = {"configurable": {"thread_id": run_id}}

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
                "decided_at": datetime.utcnow().isoformat(),
                "note": None,
            }
            print("\n--- AUTO-APPROVING (--auto-approve flag set) ---")
        else:
            decision = ask_for_approval(interrupt_payload)

        result = await app.ainvoke(Command(resume=decision), config=config)

    incident = result["incident"]
    print("\n=== final incident state ===")
    print(json.dumps(incident.model_dump(mode="json"), indent=2, default=str))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="bad_deploy", choices=list(SCENARIOS.keys()))
    parser.add_argument("--auto-approve", action="store_true",
                         help="skip the interactive y/n prompt and approve automatically")
    args = parser.parse_args()

    asyncio.run(run(args.scenario, args.auto_approve))