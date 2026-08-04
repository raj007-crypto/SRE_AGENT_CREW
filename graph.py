"""
Wires the agents together into a LangGraph state graph.

Day 1-2 scope: Detector -> Investigator only.
Day 3 will add: -> RootCause
Day 4 will add:  -> Remediator (with an interrupt() for the approval gate)
Day 5 will add:  -> Scribe

Each node takes the shared PipelineState, does its work, and returns
the fields it updated. LangGraph merges the returned dict into state.
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from agents.detector import run_detector
from agents.investigator import run_investigator
from data.fake_data_store import build_alert
from incident_schema import Incident


class PipelineState(TypedDict):
    scenario_key: str
    incident: Incident


def detector_node(state: PipelineState) -> dict:
    alert = build_alert(state["scenario_key"])
    incident = run_detector(alert)
    return {"incident": incident}


async def investigator_node(state: PipelineState) -> dict:
    incident = await run_investigator(state["incident"], state["scenario_key"])
    return {"incident": incident}


def build_graph():
    graph = StateGraph(PipelineState)

    graph.add_node("detector", detector_node)
    graph.add_node("investigator", investigator_node)

    graph.add_edge(START, "detector")
    graph.add_edge("detector", "investigator")
    graph.add_edge("investigator", END)

    return graph.compile()