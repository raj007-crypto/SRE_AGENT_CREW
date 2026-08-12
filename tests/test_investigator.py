import pytest

import data.fake_data_store as fake_data_store
from agents.investigator import run_investigator
from data.fake_data_store import SCENARIOS, build_alert, query_logs, query_metrics
from incident_schema import Incident


@pytest.mark.asyncio
async def test_chaos_mode_recovers_via_retries(monkeypatch):
    monkeypatch.setattr(fake_data_store, "CHAOS_MODE", True)
    fake_data_store._chaos_fired.clear()

    incident = Incident(id="c1", alert=build_alert("bad_deploy"))
    incident = await run_investigator(incident, "bad_deploy")

    assert incident.evidence is not None
    assert len(incident.evidence.logs) == 6
    assert len(incident.evidence.traces) == 5
    # each of the three observability sources failed exactly once before recovering
    assert len(fake_data_store._chaos_fired) == 3


def test_metric_ramp_ends_at_current_value():
    points = query_metrics("bad_deploy")
    scenario = SCENARIOS["bad_deploy"]
    assert points[0].value == scenario["threshold_breached"]
    assert points[-1].value == scenario["current_value"]


def test_error_logs_never_predate_suspect_deploy():
    deploy = SCENARIOS["bad_deploy"]["bad_commit"]
    logs = query_logs("bad_deploy")
    for entry in logs:
        if entry.level == "ERROR":
            assert entry.timestamp > deploy.deployed_at
