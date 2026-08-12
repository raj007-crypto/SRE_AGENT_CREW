"""
Scribe agent.

Job: turn everything the pipeline accumulated into a real, readable
postmortem document. This is the artifact stage -- every prior agent
produced structured data for the *next* agent to consume; this is the
first output meant for a *human* to read.

Same mock/real split as root_cause.py: with a local Ollama server running,
an LLM writes the narrative sections. Without it, a template fills in the
same structure from the raw incident data. Either way the output is a
complete, saveable Markdown file -- the mock version is intentionally
still genuinely useful, not a placeholder.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from incident_schema import Incident, IncidentStatus, Postmortem

try:
    from langsmith import traceable
except ImportError:
    def traceable(*_a, **_kw):
        def _decorator(fn):
            return fn
        return _decorator

SYSTEM_PROMPT = """You are an SRE writing a postmortem document. You will be
given the full structured record of an incident: the alert, evidence
gathered, root-cause hypothesis, the remediation that was proposed, the
human approval decision, and the outcome.

Write a clear, professional postmortem in Markdown with these sections:
# Postmortem: <short title>
## Summary (2-3 sentences)
## Timeline (bulleted, chronological)
## Root Cause
## Resolution
## Prevention (2-3 concrete suggestions to stop this recurring)

Be factual and specific -- reference actual values, commit shas, and
timestamps from the data given. Do not invent details not present in
the record. Respond with ONLY the Markdown document, no other text.
"""


def _build_user_prompt(incident: Incident) -> str:
    return f"""INCIDENT RECORD (JSON):
{json.dumps(incident.model_dump(mode="json"), indent=2, default=str)}
"""


@traceable(name="scribe_llm_call", run_type="llm")
def _call_llm(system: str, user: str, model: str) -> str:
    """Real path -- requires a running Ollama server with `model` pulled."""
    import ollama

    response = ollama.chat(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        options={"temperature": 0, "num_predict": 1200},
    )
    return response["message"]["content"].strip()


def _template_postmortem(incident: Incident) -> str:
    """
    Fallback path -- no API key needed. Fills in the same section
    structure directly from the incident record so the output is a real,
    usable document, not a stub.
    """
    alert = incident.alert
    hyp = incident.hypothesis
    proposal = incident.proposal
    approval = incident.approval
    remediation = incident.remediation

    error_count = sum(1 for l in incident.evidence.logs if l.level == "ERROR") if incident.evidence else 0
    trace_error_count = sum(1 for t in incident.evidence.traces if t.error) if incident.evidence else 0

    outcome = "resolved" if (remediation and remediation.executed) else "NOT remediated (denied or unresolved)"

    lines = [
        f"# Postmortem: {alert.alert_type.replace('_', ' ').title()} on {alert.service}",
        "",
        "## Summary",
        f"On {alert.triggered_at.isoformat()}, `{alert.service}` triggered a "
        f"{alert.alert_type} alert (`{alert.metric_name}` reached {alert.current_value}, "
        f"threshold {alert.threshold_breached}). The incident was {outcome}.",
        "",
        "## Timeline",
        f"- **{alert.triggered_at.isoformat()}** -- Alert fired: {alert.alert_type} on {alert.service}",
    ]

    if hyp and hyp.suspect_deploy:
        lines.append(
            f"- **{hyp.suspect_deploy.deployed_at.isoformat()}** -- Suspect deploy "
            f"`{hyp.suspect_deploy.commit_sha}` by {hyp.suspect_deploy.author}: "
            f"\"{hyp.suspect_deploy.message}\""
        )

    lines.append(
        f"- Investigation gathered {len(incident.evidence.logs) if incident.evidence else 0} log "
        f"entries ({error_count} errors), {len(incident.evidence.metrics) if incident.evidence else 0} "
        f"metric points, and {trace_error_count} errored trace spans."
    )

    if approval:
        decided = approval.decided_at.isoformat() if approval.decided_at else "unknown time"
        lines.append(
            f"- **{decided}** -- {'Approved' if approval.approved else 'Denied'} by "
            f"{approval.approved_by or 'unknown'}"
            + (f": {approval.note}" if approval.note else "")
        )

    if remediation:
        lines.append(f"- **{remediation.executed_at.isoformat()}** -- {remediation.output}")

    lines += [
        "",
        "## Root Cause",
        hyp.probable_cause if hyp else "Not determined.",
        f"\n*Confidence: {hyp.confidence:.0%}*" if hyp else "",
        f"\n{hyp.reasoning}" if hyp else "",
        "",
        "## Resolution",
        proposal.justification if proposal else "No remediation was proposed.",
        f"\nOutcome: {remediation.output}" if remediation else "No remediation was executed.",
        "",
        "## Prevention",
        "- Add automated tests covering the failure mode identified above before merge.",
        "- Consider a canary/staged rollout for changes to this service to catch this class "
        "of issue before it reaches 100% of traffic.",
        "- Review whether an earlier, less severe alert threshold could have caught this sooner.",
        "",
        "*[This postmortem was generated in template mode -- no Ollama LLM available. "
        "Install/start Ollama for a fuller LLM-written narrative.]*",
    ]

    return "\n".join(lines)


def run_scribe(incident: Incident) -> Incident:
    """Entry point for stage 5."""
    model = os.environ.get("OLLAMA_MODEL", "llama3.2:3b")
    mode = "TEMPLATE"
    try:
        markdown = _call_llm(SYSTEM_PROMPT, _build_user_prompt(incident), model)
        mode = "LLM"
    except Exception as e:
        print(f"[scribe] Ollama unavailable ({e}), falling back to template mode")
        markdown = _template_postmortem(incident)

    title = f"Postmortem: {incident.alert.alert_type.replace('_', ' ').title()} on {incident.alert.service}"

    incident.postmortem = Postmortem(
        title=title,
        markdown=markdown,
        generated_at=datetime.now(timezone.utc),
    )
    incident.status = IncidentStatus.DOCUMENTED

    # save to disk so it's a real artifact, not just something printed to a terminal
    os.makedirs("postmortems", exist_ok=True)
    path = f"postmortems/{incident.id}.md"
    with open(path, "w") as f:
        f.write(markdown)

    print(f"[scribe:{mode}] incident {incident.id} | postmortem written to {path}")

    return incident