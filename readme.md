# SRE agent crew

A team of AI agents that handles the first 90% of incident response automatically:
detects a problem, investigates it (in parallel, not one query at a time),
figures out the probable root cause, proposes a fix and **waits for a human
to approve anything risky**, then writes the postmortem for you.

## Why this exists

When production breaks, an engineer normally has to wake up, dig through
logs/metrics/traces by hand, correlate that with recent deploys, decide on
a fix, apply it, and write up what happened. That's 30-60 minutes of
stressful, error-prone work, often done half-asleep at 3am.

This project automates the detective work and leaves the human in control
of anything destructive. The design choice that matters most here isn't
"agents calling an LLM" -- it's the **human-in-the-loop approval gate**
before any remediation action executes.

## Status

- [x] Day 1 -- toy incident scenarios + fake observability data
- [x] Day 2 -- Detector agent + Investigator agent (parallel evidence gathering)
- [x] Day 3 -- Root-cause agent (LLM reasoning over evidence + deploy history)
- [x] Day 4 -- Remediator agent + human approval gate (CLI demo working; Slack scaffold ready)
- [x] Day 5 -- Scribe agent (automatic postmortem generation)
- [x] Day 6 -- error handling, retries, LangSmith tracing
- [ ] Day 7 -- polish, demo video, architecture diagram

## Architecture

```
Detector -> Investigator -> RootCause -> Remediator --[approval gate]--> Scribe
             (parallel:                  (proposes,
              logs/metrics/traces)        waits for
                                          human OK)
```

State flows through one shared `Incident` object (see `incident_schema.py`)
that every agent reads from and writes back to.

## Running it (current scope: stages 1-5, i.e. the full pipeline)

```bash
pip install -r requirements.txt
python main.py --scenario bad_deploy
```

The pipeline will pause partway through and print an approval prompt
in the terminal -- type `y` to approve the proposed rollback, or
anything else to deny it. Use `--auto-approve` to skip the prompt for
scripted demos. Every run writes a real postmortem to
`postmortems/<incident-id>.md`, whether the action was approved or denied.

Use `--chaos` to inject one transient failure into each of the three
parallel observability calls (logs/metrics/traces) -- you'll see them
retry automatically and the run still succeeds. This is the fastest way
to actually demo the retry logic instead of just describing it:

```bash
python main.py --scenario memory_leak --auto-approve --chaos
```

For tracing, set `LANGSMITH_TRACING=true` + `LANGSMITH_API_KEY` (copy
`.env.example` to `.env` -- `main.py` loads it automatically via
`load_dotenv()`) and every run shows up as a full trace at smith.langchain.com
-- graph structure, node timings, and the individual LLM calls -- with no
code changes needed beyond the two `@traceable` decorators already in
`root_cause.py` and `scribe.py`.

By default the Root-cause agent runs in **mock mode** (a closest-deploy
heuristic) and the approval gate runs in **CLI mode** (prints to your
terminal instead of Slack), so the whole pipeline is testable with zero
setup. To get real LLM reasoning, install [Ollama](https://ollama.com) and
pull a model (`ollama pull llama3.2:3b`). Set `OLLAMA_MODEL` to pick a
different model (e.g. `qwen2.5`, `llama3.1:8b`); otherwise the code just
tries the local Ollama server on every run and falls back to the heuristic
when it isn't there -- no code changes needed.

Real Slack approval is **not yet implemented**. `agents/approval_gate.py`
can *post* a message with Approve/Deny buttons, and `slack_server.py` is a
stub that receives the button clicks -- but the resume step (incident ->
thread_id lookup + `Command(resume=...)` against a persistent checkpointer)
is a TODO, so today only the CLI approval path (`main.py`'s
`ask_for_approval`) works end-to-end.

Available scenarios: `bad_deploy`, `memory_leak`, `bad_config` (see
`data/fake_data_store.py`). Each simulates a distinct, realistic root
cause so the later Root-cause agent has something real to reason about.

## Project layout

```
incident_schema.py        # shared state object threaded through the whole graph
data/fake_data_store.py   # simulated logs/metrics/traces/deploy history
agents/detector.py        # stage 1: triage the raw alert
agents/investigator.py    # stage 2: parallel evidence gathering
agents/root_cause.py      # stage 3: root-cause hypothesis (Ollama LLM or heuristic)
agents/remediator.py      # stage 4: propose + execute remediation
agents/approval_gate.py   # stage 4: human approval (interrupt) -- Slack is scaffold only
agents/scribe.py          # stage 5: postmortem generation (Ollama LLM or template)
graph.py                  # LangGraph wiring
main.py                   # CLI entry point
utils/retry.py            # shared tenacity retry policy
slack_server.py           # STUB -- Slack approval resume is not implemented
tests/                    # pytest suite (retries, data consistency, pipeline e2e)
```

## Design notes

- **Why LangGraph:** the approval gate in stage 4 needs the pipeline to
  pause mid-execution and resume later when a human responds. LangGraph's
  checkpointing/`interrupt()` support is built for exactly this; a simple
  linear agent chain can't do it cleanly.
- **Why parallel evidence gathering matters:** `agents/investigator.py`
  fans out three tool calls concurrently instead of sequentially. On a
  toy demo this is the difference between ~1s and ~3s; on a real
  observability stack with slower APIs, it's the difference between a
  5-second investigation and a 60-second one.
- **Two different retry strategies for two different failure modes:**
  `utils/retry.py` retries the observability calls (logs/metrics/traces)
  with exponential backoff, because their failure mode is "transient
  network blip" -- retrying the same call again is likely to work.
  `agents/root_cause.py`'s `_call_llm` instead retries with an *edited
  prompt* on malformed JSON, because the failure mode there is "the
  model didn't follow instructions" -- retrying the identical prompt
  wouldn't reliably fix that, but adding an explicit correction does.
  Using the same backoff-retry for both would be the wrong tool for the
  JSON case.
- **Failures degrade, they don't crash:** if the real LLM call in
  root_cause or scribe throws for any reason (network, auth, rate limit),
  both agents catch it and fall back to their mock/template path rather
  than taking down the whole incident response pipeline -- ironic to
  have your *incident response* system be the thing paging someone at
  3am because an API had a bad minute. Only a genuinely unrecoverable
  failure (all observability retries exhausted, etc.) bubbles up, and
  even then `main.py`'s top-level try/except reports it cleanly instead
  of a raw stack trace.
- **`--chaos` flag exists so the retry logic isn't just theoretical:**
  `data/fake_data_store.py` can inject one transient failure into each
  observability call. Without a way to trigger a failure on demand,
  retry code is unverified until it hits production -- which is exactly
  the kind of thing worth catching in a demo, not in an actual incident.
- **The approval gate is a real pause, not a fake one:** `agents/approval_gate.py`
  uses LangGraph's `interrupt()`, which actually halts graph execution --
  the process can sit there for minutes or (with a persistent checkpointer)
  even restart while waiting. Resuming requires a second `.ainvoke()` call
  with `Command(resume=...)` and the same `thread_id`. One non-obvious
  gotcha worth knowing for an interview: LangGraph re-runs a node's code
  from the top every time that node resumes from a pause -- so any side
  effect placed *before* `interrupt()` in the same node would fire twice.
  That's why "notify a human" (`notify_node`) and "wait for their decision"
  (`approval_gate_node`) are two separate graph nodes here, not one.
- **Scribe produces a real artifact either way:** `agents/scribe.py` follows
  the same mock/real split as root_cause -- template-filled Markdown
  without Ollama, LLM-written narrative with it. The template path
  is deliberately not a stub: it pulls real values (timestamps, commit
  shas, error counts) out of the incident record so the output is
  genuinely readable, not a placeholder. Known rough edge: in the denied
  path, the "Resolution" section still shows the original rollback
  justification before the "Outcome: denied" line, which reads a little
  oddly -- worth tightening the template's wording for that branch later.
- **Mock vs. real LLM reasoning:** `agents/root_cause.py` checks for a
  running local Ollama server and picks between an open-source LLM and a
  rule-based heuristic. This is a deliberate dependency-injection
  pattern -- the graph, state schema, and every downstream stage don't
  care which path ran, they just consume a `Hypothesis` object either
  way. It also means the whole pipeline is CI-testable without paying
  for API calls on every test run.
- **Simulated vs. real infra:** this repo uses synthetic data so the
  whole pipeline is runnable without any external accounts. Swapping
  `data/fake_data_store.py` for real Datadog/Prometheus/GitHub API
  clients is a drop-in replacement -- nothing upstream needs to change,
  since the Investigator only depends on the function signatures.