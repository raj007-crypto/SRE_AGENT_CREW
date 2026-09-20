"""
End-to-end smoke test for the payments service.

For every fault: reset -> measure baseline -> inject -> measure the damage and
collect evidence -> apply the fix -> confirm recovery.

Run the service with built-in traffic first:
    ENABLE_TRAFFIC=1 uvicorn payments_service.asgi:app --port 8001
then:
    python scripts/smoke_test.py --url http://localhost:8001

Exit code 0 only if every fault was (a) visibly harmful and (b) fully fixed.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter

import httpx

# scenario -> (signal metric, how to fix it)
PLAN = {
    "bad_deploy": ("error_rate", "rollback"),
    "memory_leak": ("p95", "rollback"),
    "bad_config": ("error_rate", "rollback"),
    "slow_query": ("p95", "rollback"),
    "downstream_outage": ("error_rate", "clear_fault"),
}


def signal(stats: dict, metric: str) -> float:
    o = stats["overall"]
    return o["error_rate"] if metric == "error_rate" else o["latency_ms"]["p95"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8001")
    ap.add_argument("--settle", type=float, default=12, help="seconds to observe each phase")
    ap.add_argument("--admin-token", default=None)
    ap.add_argument("--only", nargs="*", choices=list(PLAN), help="run a subset")
    args = ap.parse_args()

    headers = {"X-Admin-Token": args.admin_token} if args.admin_token else {}
    c = httpx.Client(base_url=args.url, headers=headers, timeout=30)
    window = args.settle - 1

    failures = []
    for name in args.only or PLAN:
        metric, fix = PLAN[name]
        print(f"\n=== {name}  (signal: {metric}) ===")

        c.post("/admin/reset").raise_for_status()
        time.sleep(args.settle)
        base = c.get("/stats", params={"window": window}).json()

        inj = c.post(f"/admin/inject/{name}").json()
        if "deployed" in inj:
            d = inj["deployed"]
            print(f"  injected: deploy {d['version']} ({d['commit_sha']}) by {d['author']}: {d['message']}")
        else:
            print(f"  injected: external fault '{inj['fault']}' (no deploy involved)")
        time.sleep(args.settle)
        broken = c.get("/stats", params={"window": window}).json()

        # Evidence an investigator could use
        logs = c.get("/admin/logs", params={"window": window, "level": "WARNING", "limit": 2000}).json()
        top = Counter(
            (l.get("error_message") or l["message"])[:90]
            for l in logs if l["level"] in ("ERROR", "WARNING") and l.get("status") != 402
        ).most_common(2)
        spans = c.get("/admin/traces", params={"window": window, "limit": 500}).json()
        slow_ops = Counter(s["operation"] for t in spans for s in t["spans"] if s["duration_ms"] > 500)

        truth = c.get("/admin/ground_truth").json()
        if fix == "rollback":
            c.post("/admin/rollback").raise_for_status()
        else:
            c.put(f"/admin/faults/{name}", json={"enabled": False}).raise_for_status()
        time.sleep(args.settle)
        fixed = c.get("/stats", params={"window": window}).json()
        healthy = c.get("/admin/ground_truth").json()["healthy"]

        b, x, r = signal(base, metric), signal(broken, metric), signal(fixed, metric)
        print(f"  {metric:<10} baseline={b:<8} broken={x:<8} recovered={r:<8}")
        print(f"  ground truth while broken: bug={truth['active_bug']} config={truth['config']} "
              f"external={truth['external_faults']}")
        for msg, n in top:
            print(f"  evidence x{n}: {msg}")
        if slow_ops:
            print(f"  slow spans (>500ms): {dict(slow_ops)}")

        if metric == "error_rate":
            harmful, recovered = x > 0.15, r < 0.05
        else:
            harmful, recovered = x > max(2 * b, b + 300), r < max(1.5 * b, b + 150)
        ok = harmful and recovered and healthy
        print(f"  harmful={harmful} recovered={recovered} ground_truth_healthy={healthy}  -> {'PASS' if ok else 'FAIL'}")
        if not ok:
            failures.append(name)

    print("\n" + ("ALL FAULTS PASSED" if not failures else f"FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
