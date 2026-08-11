"""
Human approval gate.

notify_human() has a side effect (sends a message), so it runs in its
OWN graph node, completed before the interrupt. LangGraph re-runs a
node's code from the top on every resume -- a side effect placed before
interrupt() in the SAME node would fire twice. Splitting these into two
nodes avoids that.

wait_for_approval() calls interrupt(), which pauses graph execution
until something calls app.ainvoke(Command(resume=<decision>), config)
with the same thread_id.
"""

from __future__ import annotations

import os

from langgraph.types import interrupt

from incident_schema import ApprovalDecision, Incident


def notify_human(incident: Incident) -> None:
    proposal = incident.proposal
    message = (
        f":rotating_light: Incident {incident.id} on `{incident.alert.service}`\n"
        f"Proposed action: {proposal.action} -> {proposal.target}\n"
        f"Why: {proposal.justification}"
    )

    if os.environ.get("SLACK_BOT_TOKEN"):
        _post_to_slack(incident.id, message)
    else:
        print("\n" + "=" * 70)
        print("[approval_gate:MOCK] No SLACK_BOT_TOKEN set -- printing instead of "
              "posting to Slack. Set SLACK_BOT_TOKEN + SLACK_APPROVAL_CHANNEL to go live.")
        print(message)
        print("=" * 70)


def _post_to_slack(incident_id: str, message: str) -> None:
    from slack_sdk import WebClient

    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    channel = os.environ["SLACK_APPROVAL_CHANNEL"]

    client.chat_postMessage(
        channel=channel,
        text=message,
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": message}},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "style": "primary",
                        "action_id": "approve_remediation",
                        "value": incident_id,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Deny"},
                        "style": "danger",
                        "action_id": "deny_remediation",
                        "value": incident_id,
                    },
                ],
            },
        ],
    )


def wait_for_approval(incident: Incident) -> ApprovalDecision:
    decision_payload = interrupt({
        "incident_id": incident.id,
        "proposal": incident.proposal.model_dump(),
        "hypothesis": incident.hypothesis.model_dump() if incident.hypothesis else None,
    })
    return ApprovalDecision(**decision_payload)