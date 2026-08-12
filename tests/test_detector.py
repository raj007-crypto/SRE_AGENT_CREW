from datetime import datetime, timezone

from agents.detector import classify_severity
from incident_schema import Alert, Severity


def _alert(alert_type: str) -> Alert:
    return Alert(
        service="checkout-api",
        alert_type=alert_type,
        metric_name="http_5xx_rate",
        threshold_breached=1.0,
        current_value=10.0,
        triggered_at=datetime.now(timezone.utc),
    )


def test_error_rate_spike_classified_critical():
    assert classify_severity(_alert("error_rate_spike")) is Severity.CRITICAL


def test_latency_spike_classified_high():
    assert classify_severity(_alert("latency_spike")) is Severity.HIGH


def test_timeout_spike_classified_high():
    assert classify_severity(_alert("timeout_spike")) is Severity.HIGH


def test_unknown_alert_type_falls_back_to_medium():
    assert classify_severity(_alert("mystery_signal")) is Severity.MEDIUM
