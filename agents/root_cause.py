"""
Root-cause agent.

Job: take the evidence bundle (logs/metrics/traces) plus deploy history
and produce a structured hypothesis -- probable cause, confidence score,
and the specific deploy it suspects. This is the first real *inference*
step in the pipeline; everything before this was retrieval.

Design note: this runs a LOCAL open-source model through Ollama (set
OLLAMA_MODEL to pick e.g. qwen2.5, llama3.2:3b, mistral). No proprietary
APIs are used anywhere. If Ollama isn't installed/running, it falls back
to a rule-based heuristic (closest deploy to the alert time = suspect) so
the graph stays runnable end-to-end without any setup. This is a
deliberate dependency-injection pattern -- swap `_call_llm` for another
provider whenever you're ready, nothing else in the pipeline needs to
change.
"""

from __future__ import annotations

import json
import os

from data.fake_data_store import get_deploy_history
from incident_schema import DeployEvent, Hypothesis, Incident, IncidentStatus

try:
    from langsmith import traceable
except ImportError:  # tracing is optional -- pipeline works fine without it
    def traceable(*_a, **_kw):
        def _decorator(fn):
            return fn
        return _decorator

SYSTEM_PROMPT = """You are an SRE root-cause analysis agent. You will be given:
1. An alert (what triggered the incident)
2. Evidence gathered from logs, metrics, and traces
3. Recent deploy history for the affected service

Your job is to identify the single most probable root cause and which
deploy (if any) most likely caused it. Respond with ONLY a JSON object,
no other text, matching this exact schema:

{
  "probable_cause": "<one sentence description of the root cause>",
  "confidence": <float between 0.0 and 1.0>,
  "suspect_commit_sha": "<commit sha from the deploy history, or null>",
  "reasoning": "<2-3 sentences explaining how the evidence supports this conclusion>"
}
"""


def _build_user_prompt(incident: Incident, deploys: list[DeployEvent]) -> str:
    error_logs = [l for l in incident.evidence.logs if l.level == "ERROR"]
    deploy_lines = "\n".join(
        f"- {d.commit_sha} by {d.author} at {d.deployed_at.isoformat()}: {d.message}"
        for d in deploys
    )
    return f"""ALERT:
service={incident.alert.service}, type={incident.alert.alert_type},
metric={incident.alert.metric_name}, current_value={incident.alert.current_value},
threshold={incident.alert.threshold_breached}, triggered_at={incident.alert.triggered_at.isoformat()}

ERROR LOGS ({len(error_logs)} of {len(incident.evidence.logs)} total):
{chr(10).join(f"- [{l.timestamp.isoformat()}] {l.message}" for l in error_logs)}

METRIC TREND (last {len(incident.evidence.metrics)} points):
{chr(10).join(f"- {m.timestamp.isoformat()}: {m.value}" for m in incident.evidence.metrics)}

TRACE ERRORS: {sum(1 for t in incident.evidence.traces if t.error)} of {len(incident.evidence.traces)} spans errored

RECENT DEPLOYS:
{deploy_lines}
"""


@traceable(name="root_cause_llm_call", run_type="llm")
def _call_llm_once(system: str, user: str, model: str) -> str:
    import ollama

    response = ollama.chat(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        format="json",
        options={"temperature": 0},
    )
    text = response["message"]["content"].strip()
    return text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()


def _call_llm(system: str, user: str, model: str, max_attempts: int = 2) -> dict:
    """
    Real path -- requires a running Ollama server with `model` pulled.
    Returns parsed JSON dict.

    LLMs occasionally don't follow the "respond with ONLY JSON" instruction
    perfectly. Rather than crash the whole pipeline on a malformed response,
    retry once with an explicit correction appended to the prompt -- cheaper
    and more reliable than a generic exponential-backoff retry here, since
    the failure mode is "wrong format", not "server unavailable".
    """
    last_error = None
    for attempt in range(1, max_attempts + 1):
        prompt = user if attempt == 1 else (
            user + "\n\nYour previous response was not valid JSON. "
            "Respond with ONLY the JSON object, no markdown, no commentary."
        )
        text = _call_llm_once(system, prompt, model)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            last_error = e
            print(f"[root_cause] LLM returned malformed JSON on attempt {attempt}, "
                  f"{'retrying' if attempt < max_attempts else 'giving up'}")

    raise ValueError(f"root_cause LLM call failed to return valid JSON after "
                      f"{max_attempts} attempts: {last_error}")


def _mock_llm(incident: Incident, deploys: list[DeployEvent]) -> dict:
    """
    Fallback path -- no Ollama server needed. Simple heuristic: the deploy
    closest in time before the alert is the suspect. This is clearly
    labeled as a mock so nobody mistakes it for real reasoning -- swap
    to _call_llm once a local Ollama server is available.
    """
    alert_time = incident.alert.triggered_at
    prior_deploys = [d for d in deploys if d.deployed_at < alert_time]
    suspect = min(prior_deploys, key=lambda d: alert_time - d.deployed_at) if prior_deploys else None

    minutes_before = (alert_time - suspect.deployed_at).total_seconds() / 60 if suspect else None
    confidence = max(0.4, 1.0 - (minutes_before / 120)) if minutes_before is not None else 0.3

    return {
        "probable_cause": f"Recent deploy '{suspect.message}' likely introduced this issue"
        if suspect else "No clear suspect deploy found in the window",
        "confidence": round(confidence, 2),
        "suspect_commit_sha": suspect.commit_sha if suspect else None,
        "reasoning": (
            f"[MOCK MODE - no Ollama server available] Deploy {suspect.commit_sha} landed "
            f"{minutes_before:.0f} minutes before the alert fired, and error logs began "
            "shortly after. Closest-deploy-in-time heuristic used instead of LLM reasoning."
            if suspect else "[MOCK MODE] No prior deploy found near the alert window."
        ),
    }


def run_root_cause(incident: Incident, scenario_key: str) -> Incident:
    """Entry point for stage 3."""
    deploys = get_deploy_history(scenario_key)
    model = os.environ.get("OLLAMA_MODEL", "llama3.2:3b")
    mode = "MOCK"

    try:
        user_prompt = _build_user_prompt(incident, deploys)
        result = _call_llm(SYSTEM_PROMPT, user_prompt, model)
        mode = "LLM"
    except Exception as e:
        # Don't let an LLM outage take down the whole incident response
        # pipeline -- degrade to the heuristic instead of failing closed.
        print(f"[root_cause] Ollama unavailable ({e}), falling back to mock mode")
        result = _mock_llm(incident, deploys)

    suspect_deploy = None
    if result.get("suspect_commit_sha"):
        suspect_deploy = next(
            (d for d in deploys if d.commit_sha == result["suspect_commit_sha"]), None
        )

    incident.hypothesis = Hypothesis(
        probable_cause=result["probable_cause"],
        confidence=result["confidence"],
        suspect_deploy=suspect_deploy,
        reasoning=result["reasoning"],
    )
    incident.status = IncidentStatus.ROOT_CAUSE_FOUND

    print(f"[root_cause:{mode}] incident {incident.id} | "
          f"cause='{incident.hypothesis.probable_cause}' "
          f"confidence={incident.hypothesis.confidence} "
          f"suspect={suspect_deploy.commit_sha if suspect_deploy else None}")

    return incident