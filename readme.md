<div align="center">

# SRE Agent Crew

### An autonomous multi-agent incident response pipeline

**Detect. Investigate. Diagnose. Propose. *Wait for a human.* Remediate. Document.**

A team of specialized AI agents that performs the detective work of a production
incident in minutes instead of hours — and deliberately steps aside for human
approval before anything destructive happens.

[Why it exists](#why-it-exists) ·
[Architecture](#architecture) ·
[Key features](#key-features) ·
[Design decisions](#design-decisions) ·
[Getting started](#getting-started) ·
[Tests](#testing) ·
[Project layout](#project-layout)

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)
![LangGraph](https://img.shields.io/badge/LangGraph-1.2-1C3C3C?style=flat-square)
![Pydantic](https://img.shields.io/badge/Pydantic-2.x-E92063?style=flat-square)
![Local LLM](https://img.shields.io/badge/LLM-Ollama%20(local)-666666?style=flat-square)
![Slack](https://img.shields.io/badge/Approval-CLI%20%2B%20Slack%20scaffold-4A154B?style=flat-square)
![Tests](https://img.shields.io/badge/tests-18%20passing-2ea44f?style=flat-square)

</div>

---

## Why it exists

When production breaks, an engineer normally has to wake up at 3am, dig through
logs, metrics, and traces by hand, correlate that with recent deploys, decide on
a fix, apply it, and write up what happened. That is 30–60 minutes of stressful,
error-prone work — often done half-asleep, while users are actively affected.

**This project automates the first 90% of that work.**

A chain of specialized agents detects the alert, gathers evidence from every
observability system *in parallel*, reasons about the root cause, proposes a
remediation, and drafts the postmortem. The one thing it refuses to do without a
human is the thing that could make things worse: **executing the fix**.

The most important design decision here is not "agents call an LLM" — it is the
**human-in-the-loop approval gate** that sits between *proposal* and *execution*.
Machines do the thinking and the legwork; humans keep the authority.

---

## Architecture

```
                          +--------------------------------------------------------------+
                          |                       SRE Agent Crew                          |
                          |                                                              |
  ALERT  --> +----------+  +--------------+  +-----------+  +-----------------------+    |
 (webhook)  | DETECTOR |->| INVESTIGATOR |->| ROOT CAUSE|->| REMEDIATOR (propose)   |    |
            +----------+  +--------------+  +-----------+  +-----------------------+    |
                  |            |  |  |            |                     |               |
                  |            |  logs  metrics   traces               |               |
                  |            |  /      /        /                     |               |
                  |            |  v      v        v                     v               |
                  |            +--------------------------------> EVIDENCE    PROPOSAL  |
                  |                                                                    |
                  |         +-----------+  +--------------+  +------------------------+ |
                  |         |   SCRIBE  |<-| REMEDIATOR   |<-|  APPROVAL GATE          | |
                  |         | (stage 5) |  |  (execute)   |  |  +-------------------+  | |
                  |         +-----------+  +--------------+  |  | human (CLI/Slack)  |  | |
                  |              ^                ^          |  +-------------------+  | |
                  |              |                |          +------------------------+ |
                  +-- POSTMORTEM +   REMEDIATION          (LangGraph interrupt/resume)  |
                     (artifact)      (only after OK)                                     |
                          +--------------------------------------------------------------+
```

The full pipeline, wired end-to-end in a LangGraph state graph (`graph.py`):

```
Detector → Investigator → RootCause → RemediatorPropose → Notify
  → ApprovalGate (interrupts, waits for a human)
  → RemediatorExecute → Scribe → END
```

One shared, typed `Incident` object (`incident_schema.py`) flows through every
stage. Each agent reads the sections it needs and writes its own section back —
logs and metrics merge into `Evidence`, evidence and deploy history become a
`Hypothesis`, the hypothesis becomes a `RemediationProposal`, the proposal waits
for an `ApprovalDecision`, and the decision becomes a `Postmortem` artifact.

---

## Key features

| Feature | What it does |
|---|---|
| **Human-in-the-loop approval gate** | The pipeline *truly pauses* mid-run via LangGraph `interrupt()` and resumes only when a human decides. Nothing destructive executes without sign-off. |
| **Parallel evidence gathering** | Logs, metrics, and traces are queried concurrently (`asyncio.gather`) — not one-at-a-time. |
| **Root-cause reasoning (local, private LLM)** | Runs a local open-source model via Ollama. No proprietary APIs, no data leaves the machine. |
| **Graceful degradation, not crashes** | If the LLM is unavailable, agents fall back to deterministic heuristics — the pipeline never dies from a bad API minute. |
| **Two retry strategies, two failure modes** | Exponential backoff for transient API blips; prompt-edit-and-retry for malformed LLM JSON. Each tuned to its specific failure mode. |
| **Automatic postmortem generation** | Scribe writes a complete, timestamped Markdown document — even on the denied path. |
| **Distributed tracing out of the box** | `@traceable` on every LLM call feeds LangSmith: graph structure, node timings, and LLM I/O. |
| **Chaos mode** | `--chaos` injects a transient failure per observability call so the retry logic is *demonstrably* exercised, not theoretical. |
| **Zero-setup deterministic demo** | Seeded synthetic data + mock modes mean the whole pipeline runs end-to-end without any external accounts. |

---

## Design decisions

This section exists because the interesting engineering is not *that* agents call
an LLM — it is *how* the pieces are shaped around real operational constraints.

### Why LangGraph

The approval gate requires the pipeline to **pause mid-execution and resume later**
when a human responds. LangGraph's checkpointing and `interrupt()` are built for
exactly this. A simple linear agent chain cannot do it cleanly — the graph state
must survive a pause that can last minutes, or (with a persistent checkpointer)
even a process restart.

### Why parallel evidence gathering matters

`agents/investigator.py` fans out three tool calls concurrently instead of
sequentially. On this toy demo that is the difference between ~1s and ~3s; on a
real observability stack with slower APIs, it is the difference between a 5-second
investigation and a 60-second one. Response time is everything during an incident.

### Two retry strategies for two different failure modes

- **`utils/retry.py`** retries observability calls (logs/metrics/traces) with
  exponential backoff, because their failure mode is *transient network blip* —
  retrying the same call again is likely to work.
- **`agents/root_cause.py`**'s `_call_llm` instead retries with an **edited
  prompt** when the model returns malformed JSON, because that failure mode is
  *the model didn't follow instructions* — replaying the identical prompt
  wouldn't fix it, but an explicit correction does.

Using the same retry for both would be the wrong tool for the JSON case.

### The approval gate is a real pause, not a fake one

`agents/approval_gate.py` uses LangGraph's `interrupt()`, which actually halts
execution. Resuming requires a second `.ainvoke()` call with
`Command(resume=...)` and the same `thread_id`.

One non-obvious gotcha: **LangGraph re-runs a node's code from the top every time
it resumes from a pause** — so any side effect placed *before* `interrupt()` in the
same node would fire twice. That is why "notify a human" (`notify_node`) and "wait
for their decision" (`approval_gate_node`) are **two separate graph nodes**, not one.

### Failures degrade, they don't crash

If the real LLM call throws for any reason (network, auth, rate limit), both
`root_cause.py` and `scribe.py` catch it and fall back to their mock/template
paths rather than taking down the whole incident response pipeline. It would be
deeply ironic for an *incident response* system to be the thing paging someone at
3am because an API had a bad minute.

Only a genuinely unrecoverable failure (all retries exhausted, a bug) bubbles up —
and even then, `main.py`'s top-level handler records it on the `Incident`
(`status=FAILED`, error captured) and reports it cleanly instead of a raw stack
trace. **Every run produces an artifact, even failed ones.**

### Mock vs. real LLM reasoning (dependency injection)

`agents/root_cause.py` probes for a running local Ollama server and picks between
an open-source LLM and a rule-based heuristic (closest-deploy-to-alert-time). The
graph, state schema, and every downstream stage don't care which path ran — they
consume a `Hypothesis` object either way. This makes the entire pipeline
CI-testable without paying for API calls on every test run, while still being a
drop-in swap to a real provider.

### Scribe produces a real artifact either way

The template path in `agents/scribe.py` is deliberately not a stub: it pulls real
timestamps, commit shas, and error counts out of the incident record, so the
output is genuinely readable, not a placeholder. The LLM path writes a fuller
narrative with the same structure. Both land in `postmortems/<incident-id>.md`.

### Simulated vs. real infrastructure

The repo uses synthetic data so the whole pipeline runs without external accounts.
Swapping `data/fake_data_store.py` for real Datadog/Prometheus/GitHub API clients
is a drop-in replacement — nothing upstream needs to change, since the Investigator
only depends on the function signatures.

---

## Tech stack

| Layer | Technology |
|---|---|
| Orchestration | [LangGraph](https://www.langchain.com/langgraph) (state graph, checkpointing, interrupts) |
| Data model | Pydantic v2 (typed shared state, `use_enum_values`) |
| Agents | 6 specialized roles (Detector, Investigator, Root Cause, Remediator, Approval, Scribe) |
| LLM | Local open-source via [Ollama](https://ollama.com) (e.g. `llama3.2:3b`) |
| Observability | [LangSmith](https://smith.langchain.com) tracing (`@traceable`) |
| Retry policy | [Tenacity](https://github.com/jd/tenacity) (exponential backoff) |
| Human approval | CLI prompt + Slack SDK scaffold (`slack_server.py`) |
| Backend (stub) | FastAPI / uvicorn (Slack interactivity receiver — scaffold only) |
| Tests | pytest + pytest-asyncio |

---

## Getting started

### Prerequisites

- Python 3.11+
- *(Optional)* [Ollama](https://ollama.com) with a model pulled for real LLM reasoning

### Installation

```bash
pip install -r requirements.txt
```

### Run the full pipeline

```bash
python main.py --scenario bad_deploy
```

The pipeline will pause at the approval gate and print a prompt in your
terminal — type `y` to approve the proposed rollback, anything else to deny it.
Every run writes a real postmortem to `postmortems/<incident-id>.md`, whether the
action was approved or denied.

### Useful flags

| Flag | Effect |
|---|---|
| `--scenario <key>` | Choose the incident: `bad_deploy`, `memory_leak`, `bad_config` |
| `--auto-approve` | Skip the interactive prompt (for scripted demos) |
| `--chaos` | Inject one transient failure per observability call to demo the retry logic |

```bash
# Fastest way to demo retries end-to-end
python main.py --scenario memory_leak --auto-approve --chaos
```

### Enable real LLM reasoning (optional)

```bash
ollama pull llama3.2:3b
```

Set `OLLAMA_MODEL` to pick a different model (e.g. `qwen2.5`, `llama3.1:8b`).
If Ollama isn't running, the code just falls back to the heuristic — no code
changes needed.

### Enable LangSmith tracing (optional)

Copy `.env.example` to `.env` and set `LANGSMITH_TRACING=true` +
`LANGSMITH_API_KEY`. Every run appears as a full trace at smith.langchain.com —
graph structure, node timings, and the individual LLM calls — via the two
`@traceable` decorators already in `root_cause.py` and `scribe.py`.

---

## The three scenarios

Each simulates a distinct, realistic root cause so the later stages have
something genuine to reason about:

| Scenario | What breaks | Suspect deploy | Alert type |
|---|---|---|---|
| `bad_deploy` | Null-pointer bug in payment validation | `a1b2c3d` by `jsmith` | `error_rate_spike` |
| `memory_leak` | Cache that's never evicted → GC pressure | `e4f5g6h` by `rpatel` | `latency_spike` |
| `bad_config` | DB connection pool dropped too low | `i7j8k9l` by `tchen` | `timeout_spike` |

Data is seeded deterministically (`random.seed(7)`), so demos are reproducible.

---

## Testing

```bash
pytest
```

**18 tests, all passing.** The suite covers:

- **Detector** — severity classification rules and unknown-alert fallback
- **Investigator** — chaos-mode recovery via retries; data consistency (error logs
  never predate the suspect deploy; metric ramp ends exactly at the breached value)
- **Root cause** — confidence scales with deploy recency; floor at 0.4; low
  confidence when no prior deploy exists
- **Remediator** — confident suspect → `rollback`; no suspect → `page_oncall`
- **Approval gate** — missing Slack config degrades gracefully, never crashes
- **Pipeline (end-to-end)** — approved path ends `DOCUMENTED` with a real
  postmortem file on disk; denied path still writes a postmortem
- **Failure path** — a crashing node marks the incident `FAILED` with the error
  captured, instead of losing the run

---

## Project layout

```
incident_schema.py        # shared typed state threaded through the whole graph
graph.py                  # LangGraph wiring, checkpointing, serde registration
main.py                   # CLI entry point + human-in-the-loop approval prompt
agents/
  detector.py             # stage 1: triage the raw alert (rule-based, fast)
  investigator.py         # stage 2: parallel evidence gathering (fan-out/fan-in)
  root_cause.py           # stage 3: hypothesis (Ollama LLM or heuristic fallback)
  remediator.py           # stage 4: propose + execute remediation
  approval_gate.py        # stage 4: notify human + interrupt() for the decision
  scribe.py               # stage 5: postmortem generation (LLM or template)
data/fake_data_store.py   # simulated logs/metrics/traces/deploy history + chaos mode
utils/retry.py            # shared tenacity retry policy
slack_server.py           # STUB: Slack interactivity receiver (resume is a TODO)
tests/                    # pytest suite (retries, data consistency, e2e pipeline)
postmortems/              # generated postmortem artifacts
```

---

## Status & roadmap

| Day | Deliverable | Status |
|---|---|---|
| 1 | Toy incident scenarios + fake observability data | Done |
| 2 | Detector + Investigator (parallel evidence gathering) | Done |
| 3 | Root-cause agent (LLM reasoning over evidence + deploy history) | Done |
| 4 | Remediator + human approval gate | Done |
| 5 | Scribe agent (automatic postmortem generation) | Done |
| 6 | Error handling, retries, LangSmith tracing | Done |
| 7 | Polish, demo video, architecture diagram | Pending |
| — | **Real Slack approval end-to-end** (persistent checkpointer + resume from `slack_server.py`) | **Next up** |

### Known rough edge

In the denied path, the postmortem's *Resolution* section still shows the original
rollback justification before the *Outcome: denied* line — a wording wrinkle in the
template branch worth tightening.

---

## Honest scope note

This repo is a **fully runnable simulation** of the incident response pipeline.
It uses synthetic observability data so the entire flow is demo-able and
CI-testable with zero external accounts. The Slack approval path is scaffolded but
not wired end-to-end (the resume step needs a persistent checkpointer). These are
deliberate boundaries for a self-contained, verifiable build — and each seam was
designed to be replaced by a real production component without touching upstream
code.
