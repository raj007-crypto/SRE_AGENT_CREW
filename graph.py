"""
Wires the agents together into a LangGraph state graph.

Pipeline (all stages wired end-to-end):
    Detector -> Investigator -> RootCause -> RemediatorPropose -> Notify
    -> ApprovalGate (interrupt) -> RemediatorExecute -> Scribe

    Day 1-2: Detector -> Investigator
    Day 3:   -> RootCause
    Day 4:   -> RemediatorPropose -> Notify -> ApprovalGate -> RemediatorExecute
    Day 5:   -> Scribe
    Day 6:   retries (utils/retry.py), --chaos mode, LangSmith tracing, and a
             top-level failure handler in main.py that records FAILED incidents.

Each node takes the shared PipelineState, does its work, and returns
the fields it updated. LangGraph merges the returned dict into state.

The approval gate needs a checkpointer -- without one, LangGraph has nowhere
to persist state while the graph is paused waiting for a human to respond,
and `interrupt()` would fail. MemorySaver is fine for the CLI demo (everything
happens in one process). In production, swap it for a persistent checkpointer
(Postgres/Sqlite) since a real Slack approval might come minutes after this
process has restarted. Keep the custom incident_schema types registered on
the serializer (see get_checkpointer) or checkpoint deserialization will
silently fall back to raw dicts.
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph

from agents.approval_gate import notify_human, wait_for_approval
from agents.detector import run_detector
from agents.investigator import run_investigator
from agents.remediator import run_remediator_execute, run_remediator_propose
from agents.root_cause import run_root_cause
from agents.scribe import run_scribe
from data.fake_data_store import build_alert
from incident_schema import (
    Alert,
    ApprovalDecision,
    DeployEvent,
    Evidence,
    Hypothesis,
    Incident,
    IncidentStatus,
    LogEntry,
    MetricPoint,
    Postmortem,
    RemediationProposal,
    RemediationResult,
    Severity,
    TraceSpan,
)

# Custom types stored in pipeline state. The checkpoint serializer must be
# told about them explicitly or msgpack deserialization warns (and will be
# blocked in a future LangGraph version). Registering them now keeps a future
# swap to a persistent checkpointer from silently breaking.
_CHECKPOINT_SERDE_TYPES = (
    Alert,
    ApprovalDecision,
    DeployEvent,
    Evidence,
    Hypothesis,
    Incident,
    IncidentStatus,
    LogEntry,
    MetricPoint,
    Postmortem,
    RemediationProposal,
    RemediationResult,
    Severity,
    TraceSpan,
)


class PipelineState(TypedDict):
    scenario_key: str
    run_id: str
    incident: Incident


def detector_node(state: PipelineState) -> dict:
    alert = build_alert(state["scenario_key"])
    incident = run_detector(alert, incident_id=state["run_id"])
    return {"incident": incident}


async def investigator_node(state: PipelineState) -> dict:
    incident = await run_investigator(state["incident"], state["scenario_key"])
    return {"incident": incident}


def root_cause_node(state: PipelineState) -> dict:
    incident = run_root_cause(state["incident"], state["scenario_key"])
    return {"incident": incident}


def remediator_propose_node(state: PipelineState) -> dict:
    incident = run_remediator_propose(state["incident"])
    return {"incident": incident}


def notify_node(state: PipelineState) -> dict:
    notify_human(state["incident"])
    return {}


def approval_gate_node(state: PipelineState) -> dict:
    incident = state["incident"]
    incident.approval = wait_for_approval(incident)
    return {"incident": incident}


def remediator_execute_node(state: PipelineState) -> dict:
    incident = state["incident"]
    incident = run_remediator_execute(incident, incident.approval)
    return {"incident": incident}


def scribe_node(state: PipelineState) -> dict:
    incident = run_scribe(state["incident"])
    return {"incident": incident}


def get_checkpointer() -> MemorySaver:
    serde = JsonPlusSerializer(allowed_msgpack_modules=_CHECKPOINT_SERDE_TYPES)
    return MemorySaver(serde=serde)


def build_graph(checkpointer=None):
    graph = StateGraph(PipelineState)

    graph.add_node("detector", detector_node)
    graph.add_node("investigator", investigator_node)
    graph.add_node("root_cause", root_cause_node)
    graph.add_node("remediator_propose", remediator_propose_node)
    graph.add_node("notify", notify_node)
    graph.add_node("approval_gate", approval_gate_node)
    graph.add_node("remediator_execute", remediator_execute_node)
    graph.add_node("scribe", scribe_node)

    graph.add_edge(START, "detector")
    graph.add_edge("detector", "investigator")
    graph.add_edge("investigator", "root_cause")
    graph.add_edge("root_cause", "remediator_propose")
    graph.add_edge("remediator_propose", "notify")
    graph.add_edge("notify", "approval_gate")
    graph.add_edge("approval_gate", "remediator_execute")
    graph.add_edge("remediator_execute", "scribe")
    graph.add_edge("scribe", END)

    return graph.compile(checkpointer=checkpointer or get_checkpointer())
