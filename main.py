"""
Run the pipeline against a simulated incident scenario.

Usage:
    python main.py --scenario bad_deploy
    python main.py --scenario memory_leak
    python main.py --scenario bad_config
"""

from __future__ import annotations

import argparse
import asyncio
import json

from data.fake_data_store import SCENARIOS
from graph import build_graph


async def run(scenario_key: str) -> None:
    if scenario_key not in SCENARIOS:
        raise SystemExit(f"unknown scenario '{scenario_key}'. choices: {list(SCENARIOS)}")

    print(f"\n=== simulating incident: {SCENARIOS[scenario_key]['description']} ===\n")

    app = build_graph()
    result = await app.ainvoke({"scenario_key": scenario_key, "incident": None})

    incident = result["incident"]
    print("\n=== final incident state (stage 1-2) ===")
    print(json.dumps(incident.model_dump(mode="json"), indent=2, default=str))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="bad_deploy", choices=list(SCENARIOS.keys()))
    args = parser.parse_args()

    asyncio.run(run(args.scenario))