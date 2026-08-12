"""
Remediator agent.

Job: propose a specific remediation action based on the root-cause
hypothesis, and (separately, in graph.py) execute it -- but ONLY after
a human approves. This file intentionally does NOT contain the
approval-gate logic itself (that's `approval_gate.py`) so that the
interrupt() call lives in its own minimal graph node with no side
effects before it. That matters: if you resume after an interrupt,
LangGraph re-runs the *entire node function* from the top -- so any
code before interrupt() in the same node re-executes. Keeping "build
the proposal" and "wait for a human" as two separate nodes avoids ever
double-running side effects.
"""
from __future__ import annotations

from datetime import datetime, timezone

from incident_schema import (
    ApprovalDecision,
    Incident,
    IncidentStatus,
    RemediationProposal,
    RemediationResult,
)


def build_proposal(incident: Incident) -> RemediationProposal:
    hyp = incident.hypothesis
    if hyp and hyp.suspect_deploy:
        return RemediationProposal(
            action="rollback",
            target=hyp.suspect_deploy.commit_sha,
            justification=(
                f"Root-cause agent identified '{hyp.probable_cause}' with "
                f"{hyp.confidence:.0%} confidence, pointing at deploy "
                f"{hyp.suspect_deploy.commit_sha} ('{hyp.suspect_deploy.message}'). "
                "Rolling back this deploy is the safest immediate mitigation."
            ),
        )
    # low-confidence / no suspect deploy -> don't guess, escalate instead
    return RemediationProposal(
        action="page_oncall",
        target=incident.alert.service,
        justification=(
            "No confident suspect deploy was identified. Escalating to a human "
            "on-call engineer instead of guessing at a fix."
        ),
    )


def run_remediator_propose(incident: Incident) -> Incident:
    """Stage 4a: build the proposal and attach it to state."""
    incident.proposal = build_proposal(incident)
    incident.status = IncidentStatus.AWAITING_APPROVAL

    print(f"[remediator] incident {incident.id} | proposing action='{incident.proposal.action}' "
          f"target='{incident.proposal.target}'")

    return incident


def run_remediator_execute(incident: Incident, approval: ApprovalDecision) -> Incident:
    """Stage 4c: act on the human's decision. Only ever called after approval_gate."""
    incident.approval = approval

    if not approval.approved:
        incident.status = IncidentStatus.DENIED
        incident.remediation = RemediationResult(
            executed=False,
            output=f"Action denied by {approval.approved_by or 'operator'}: "
                   f"{approval.note or 'no reason given'}",
            executed_at=datetime.now(timezone.utc),
        )
        print(f"[remediator] incident {incident.id} | DENIED by {approval.approved_by}")
        return incident

    output = f"Executed '{incident.proposal.action}' on '{incident.proposal.target}' (simulated)"
    incident.remediation = RemediationResult(
        executed=True, output=output, executed_at=datetime.now(timezone.utc)
    )
    incident.status = IncidentStatus.REMEDIATED

    print(f"[remediator] incident {incident.id} | APPROVED by {approval.approved_by} -> {output}")

    return incident