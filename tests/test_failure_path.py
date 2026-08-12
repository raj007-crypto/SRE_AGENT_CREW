import pytest

import graph as graph_module
from graph import build_graph, get_checkpointer
from incident_schema import IncidentStatus
from main import _mark_failed, _new_failed_incident


@pytest.mark.asyncio
async def test_pipeline_failure_marks_incident_failed(monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated node failure")

    # graph.py imported run_investigator into its own namespace, so patch
    # the reference the graph node actually calls
    monkeypatch.setattr(graph_module, "run_investigator", _boom)

    app = build_graph(checkpointer=get_checkpointer())
    run_id = "failtest"
    config = {"configurable": {"thread_id": run_id}}

    with pytest.raises(RuntimeError):
        await app.ainvoke(
            {"scenario_key": "bad_deploy", "run_id": run_id, "incident": None},
            config=config,
        )

    incident = _mark_failed(app, config, run_id, "bad_deploy", RuntimeError("simulated node failure"))
    assert incident.status == IncidentStatus.FAILED
    assert "simulated node failure" in incident.error


def test_new_failed_incident_used_when_no_state_exists():
    incident = _new_failed_incident("nofail", "bad_deploy")
    assert incident.status == IncidentStatus.FAILED
    assert incident.error
    assert incident.alert.alert_type == "pipeline_failure"
