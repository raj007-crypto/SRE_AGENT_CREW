from datetime import datetime, timezone

import pytest
from langgraph.types import Command

from graph import build_graph, get_checkpointer
from incident_schema import IncidentStatus


def _force_mock_llms(monkeypatch):
    """Make both LLM paths raise so the pipeline uses its mock/template
    fallbacks -- keeps tests fast and deterministic (no Ollama needed)."""
    import agents.root_cause as root_cause
    import agents.scribe as scribe

    def _fail(*args, **kwargs):
        raise RuntimeError("test: forcing mock/template fallback")

    monkeypatch.setattr(root_cause, "_call_llm", _fail)
    monkeypatch.setattr(scribe, "_call_llm", _fail)


def _start_run(run_id: str, scenario_key: str = "bad_deploy"):
    app = build_graph(checkpointer=get_checkpointer())
    config = {"configurable": {"thread_id": run_id}}
    return app, config


async def _run_to_decision(app, config, run_id, approved: bool):
    result = await app.ainvoke(
        {"scenario_key": "bad_deploy", "run_id": run_id, "incident": None},
        config=config,
    )
    assert "__interrupt__" in result

    decision = {
        "approved": approved,
        "approved_by": "test",
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "note": None if approved else "denied in test",
    }
    return await app.ainvoke(Command(resume=decision), config=config)


@pytest.mark.asyncio
async def test_full_pipeline_approved_ends_documented(monkeypatch, tmp_path):
    _force_mock_llms(monkeypatch)
    monkeypatch.chdir(tmp_path)  # scribe writes postmortems/ into the temp dir

    run_id = "pipetest"
    app, config = _start_run(run_id)
    result = await _run_to_decision(app, config, run_id, approved=True)

    incident = result["incident"]
    assert incident.status == IncidentStatus.DOCUMENTED
    assert incident.remediation is not None and incident.remediation.executed is True
    assert incident.postmortem is not None and incident.postmortem.markdown
    assert (tmp_path / "postmortems" / f"{run_id}.md").exists()


@pytest.mark.asyncio
async def test_full_pipeline_denied_still_writes_postmortem(monkeypatch, tmp_path):
    _force_mock_llms(monkeypatch)
    monkeypatch.chdir(tmp_path)

    run_id = "piperun2"
    app, config = _start_run(run_id)
    result = await _run_to_decision(app, config, run_id, approved=False)

    incident = result["incident"]
    assert incident.remediation is not None and incident.remediation.executed is False
    assert incident.postmortem is not None and incident.postmortem.markdown
