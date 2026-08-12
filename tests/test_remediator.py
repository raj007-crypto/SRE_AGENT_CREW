from datetime import datetime, timedelta, timezone

from agents.remediator import build_proposal
from incident_schema import Alert, DeployEvent, Hypothesis, Incident


def _incident(suspect_sha: str | None) -> Incident:
    now = datetime.now(timezone.utc)
    return Incident(
        id="t1",
        alert=Alert(
            service="checkout-api",
            alert_type="error_rate_spike",
            metric_name="http_5xx_rate",
            threshold_breached=1.0,
            current_value=10.0,
            triggered_at=now,
        ),
        hypothesis=Hypothesis(
            probable_cause="bad deploy introduced the issue",
            confidence=0.9,
            suspect_deploy=(
                DeployEvent(
                    commit_sha=suspect_sha,
                    author="jsmith",
                    message="Refactor payment validation logic",
                    deployed_at=now - timedelta(minutes=5),
                )
                if suspect_sha
                else None
            ),
            reasoning="evidence points at the recent deploy",
        ),
    )


def test_confident_suspect_deploy_proposes_rollback():
    proposal = build_proposal(_incident("a1b2c3d"))
    assert proposal.action == "rollback"
    assert proposal.target == "a1b2c3d"
    assert "a1b2c3d" in proposal.justification


def test_no_suspect_deploy_proposes_page_oncall():
    proposal = build_proposal(_incident(None))
    assert proposal.action == "page_oncall"
    assert proposal.target == "checkout-api"
    assert "on-call" in proposal.justification.lower()
