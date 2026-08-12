"""
Scaffolding for the real Slack approval flow -- NOT YET IMPLEMENTED.

This file exists so the README's reference to `slack_server.py` is honest
about what's missing. It receives Slack's interactivity POST payload and
parses the button click, but the actual resume step is a TODO.

What's needed to finish this (see agents/approval_gate.py for the same list):
  1. A store mapping incident_id -> LangGraph thread_id (today main.py just
     uses the incident id AS the thread_id, so they're the same value).
  2. A PERSISTENT checkpointer (Postgres/SQLite). MemorySaver is in-process
     only, so a main.py run and this server can't share a graph run.
  3. The resume call once a human clicks a button:
         app.ainvoke(
             Command(resume={"approved": <bool>, "approved_by": <str>,
                             "decided_at": <iso>, "note": <str|None>}),
             {"configurable": {"thread_id": <thread_id>}},
         )

Run (dev only): uvicorn slack_server:app --reload
"""

from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="sre-agent-crew slack approval (stub)")


@app.post("/slack/interactions")
async def slack_interactions(request: Request) -> JSONResponse:
    # Slack sends the interactivity payload as a JSON string inside a
    # urlencoded form field named "payload".
    form = await request.form()
    raw = form.get("payload")
    if not raw:
        return JSONResponse({"ok": False, "error": "missing payload"})

    data = json.loads(raw)
    action = (data.get("actions") or [{}])[0]
    action_id = action.get("action_id")
    incident_id = action.get("value")

    print(f"[slack_server:STUB] received action_id={action_id} incident_id={incident_id}")

    # TODO: look up thread_id for incident_id, build the graph with a
    # persistent checkpointer, and run:
    #   app.ainvoke(Command(resume={...}), {"configurable": {"thread_id": thread_id}})

    # Must ACK quickly or Slack retries; the actual resume is not done here.
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
