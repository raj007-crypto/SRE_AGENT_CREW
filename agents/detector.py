"""
Detector agent.

Job: take a raw alert and decide whether it's worth waking the rest of
the pipeline up for. Deliberately narrow and fast -- no deep reasoning,
no LLM call needed for the core logic, just triage rules. (In a fuller
version you'd add an LLM check for "is this a known-flaky alert",
but keeping this rule-based keeps stage 1 fast and cheap, which mirrors
how real detection systems work -- the expensive reasoning happens later.)
"""

from __future__ import annotations

from incident_schema import Alert, Incident, IncidentStatus, Severity

# thresholds that decide whether an alert is worth escalating
SEVERITY_RULES = {
    "error_rate_spike": Severity.CRITICAL,
    "latency_spike": Severity.HIGH,
    "timeout_spike": Severity.HIGH,
}


def classify_severity(alert: Alert) -> Severity:
    return SEVERITY_RULES.get(alert.alert_type, Severity.MEDIUM)


def run_detector(alert: Alert) -> Incident:
    """
    Entry point for stage 1. Takes a raw alert, returns a freshly
    created Incident object ready to be handed to the Investigator.
    """
    severity = classify_severity(alert)

    incident = Incident(
        status=IncidentStatus.INVESTIGATING,
        alert=alert,
    )

    print(f"[detector] incident {incident.id} opened | "
          f"service={alert.service} type={alert.alert_type} "
          f"severity={severity.value} "
          f"value={alert.current_value} (threshold={alert.threshold_breached})")

    return incident