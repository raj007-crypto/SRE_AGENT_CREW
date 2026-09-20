# payments_service: the target system

A small **fake** payments API that the SRE Agent Crew investigates. No real
money or cards: just enough behaviour to produce realistic logs, metrics and
traces, and to break in ways that are genuinely observable.

## Run it

```bash
# service + built-in synthetic traffic (recommended)
ENABLE_TRAFFIC=1 uvicorn payments_service.asgi:app --port 8001

# or run traffic separately, against any URL
uvicorn payments_service.asgi:app --port 8001
python -m payments_service.traffic_generator --url http://localhost:8001 --rps 15
```

Break something and watch it:

```bash
curl -X POST localhost:8001/admin/inject/bad_deploy
curl localhost:8001/stats?window=10        # error_rate jumps
curl "localhost:8001/admin/logs?level=ERROR&window=10&limit=3"
curl -X POST localhost:8001/admin/rollback  # recovers
```

Full check of every fault: `python scripts/smoke_test.py --url http://localhost:8001`

## The five faults

| Scenario | Cause | What is observable | Correct fix |
|---|---|---|---|
| `bad_deploy` | Real `None.get()` in payment validation | ~60% of `/pay` return 500; `AttributeError` + stack in logs | rollback |
| `memory_leak` | Cache that never evicts | p95 latency ramps; memory gauge grows; GC-pause warnings; new slow `merchant_cache.lookup` span | rollback |
| `bad_config` | DB pool size 10 -> 1 | 504s: "timed out waiting for a database connection (pool_size=1)" | rollback |
| `slow_query` | Missing index on refunds | `/refund` only: ~2s latency; "slow query ... seq scan" warnings | rollback |
| `downstream_outage` | Upstream bank gateway down | 502s "bank-gateway returned 503"; **no deploy involved** | page on-call / clear fault |

`downstream_outage` is deliberately not fixable by rollback, so the crew's
"low confidence -> page a human" path is tested against something real.

## Endpoints

Public: `POST /pay`, `POST /refund`, `GET /health`, `GET /metrics` (Prometheus), `GET /stats?window=60`.

Admin (send `X-Admin-Token` if `ADMIN_TOKEN` is set):
`GET /admin/{state,deploys,logs,traces,scenarios,ground_truth}`,
`POST /admin/{deploy,rollback,inject/<scenario>,reset}`, `PUT /admin/faults/<name>`.

## Rules for the agents (Day 2+)

* Read only `/admin/deploys`, `/admin/logs`, `/admin/traces`, `/metrics`, `/stats`.
* **Never** call `/admin/ground_truth`. It names the injected bug and exists for
  tests and the demo dashboard. The deploy history served to agents has the
  hidden `bug` label stripped, so the cause must be inferred from evidence.

## Config (env vars)

`LATENCY_SCALE` (1.0; 0 = instant), `ADMIN_TOKEN`, `ENABLE_TRAFFIC`, `TRAFFIC_RPS` (15),
`LOG_FILE`, `SEED`.
