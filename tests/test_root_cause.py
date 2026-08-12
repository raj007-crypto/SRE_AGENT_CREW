from datetime import datetime, timedelta, timezone

from agents.root_cause import _mock_llm
from incident_schema import Alert, DeployEvent, Incident


def _incident(alert_time: datetime) -> Incident:
    return Incident(
        id="t1",
        alert=Alert(
            service="checkout-api",
            alert_type="error_rate_spike",
            metric_name="http_5xx_rate",
            threshold_breached=1.0,
            current_value=10.0,
            triggered_at=alert_time,
        ),
    )


def _deploys(alert_time: datetime, minutes_before: list[int]) -> list[DeployEvent]:
    return [
        DeployEvent(
            commit_sha=f"c{i}",
            author="jsmith",
            message=f"commit {i}",
            deployed_at=alert_time - timedelta(minutes=m),
        )
        for i, m in enumerate(minutes_before)
    ]


def test_confidence_scales_with_recency():
    alert_time = datetime.now(timezone.utc)
    close = _mock_llm(_incident(alert_time), _deploys(alert_time, [10]))
    far = _mock_llm(_incident(alert_time), _deploys(alert_time, [90]))
    assert close["confidence"] > far["confidence"]
    assert close["suspect_commit_sha"] == "c0"


def test_confidence_floored_at_0_4():
    alert_time = datetime.now(timezone.utc)
    result = _mock_llm(_incident(alert_time), _deploys(alert_time, [240]))
    assert result["confidence"] == 0.4


def test_no_prior_deploy_yields_low_confidence_and_no_suspect():
    alert_time = datetime.now(timezone.utc)
    # all deploys land AFTER the alert, so no prior deploy exists
    result = _mock_llm(_incident(alert_time), _deploys(alert_time, [-5]))
    assert result["suspect_commit_sha"] is None
    assert result["confidence"] == 0.3
