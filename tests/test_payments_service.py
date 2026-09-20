"""
Tests for the fake payments service (Day 1).

These verify that each fault is *real* and *observable*: it changes the
service's behaviour, leaves evidence in logs/metrics/traces, and is fully
undone by the right fix. They run in-process (no network) with simulated
delays turned down so the suite stays fast.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import pytest_asyncio

from payments_service.app import create_app
from payments_service.settings import Settings
from payments_service.traffic_generator import TrafficGenerator

GOOD_BILLING = {"zip": "94107", "country": "US"}


def make_payload(billing: bool = True, **overrides) -> dict:
    body = {
        "amount": 42.5,
        "currency": "USD",
        "customer_id": "cus_00001",
        "card_token": "tok_abcdef01",
    }
    if billing:
        body["billing"] = GOOD_BILLING
    body.update(overrides)
    return body


REFUND = {"transaction_id": "txn_0000000001", "customer_email": "a@example.com"}


def build(**overrides):
    """A deterministic, noise-free, instant service unless a test opts in."""
    kw = dict(latency_scale=0.0, baseline_error_rate=0.0, card_decline_rate=0.0, seed=1)
    kw.update(overrides)
    app = create_app(Settings(**kw))
    app.state.service.telemetry.silence_stdout()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return app, client


@pytest_asyncio.fixture
async def svc():
    app, client = build()
    yield app.state.service, client
    await client.aclose()


async def burst(client, n, path="/pay", body=None, billing=True):
    payload = body or make_payload(billing=billing)
    rs = await asyncio.gather(*[client.post(path, json=payload) for _ in range(n)])
    return [r.status_code for r in rs], rs


# ---------------------------------------------------------------- baseline
@pytest.mark.asyncio
async def test_healthy_service_returns_200_and_no_errors(svc):
    service, c = svc
    codes, _ = await burst(c, 30)
    assert set(codes) == {200}
    assert (await c.get("/stats")).json()["overall"]["errors_5xx"] == 0
    assert (await c.get("/health")).json()["status"] == "ok"
    assert (await c.get("/admin/ground_truth")).json()["healthy"] is True


@pytest.mark.asyncio
async def test_client_errors_are_4xx_and_not_counted_as_outage(svc):
    _, c = svc
    r = await c.post("/pay", json={"currency": "USD"})
    assert r.status_code == 400
    r = await c.post("/pay", content=b"not json")
    assert r.status_code == 400
    assert (await c.get("/stats")).json()["overall"]["errors_5xx"] == 0


@pytest.mark.asyncio
async def test_card_declines_are_402_business_errors_not_5xx():
    app, c = build(card_decline_rate=1.0)
    codes, _ = await burst(c, 10)
    assert set(codes) == {402}
    assert (await c.get("/stats")).json()["overall"]["error_rate"] == 0.0
    await c.aclose()


# --------------------------------------------------------------- bad_deploy
@pytest.mark.asyncio
async def test_bad_deploy_crashes_only_requests_missing_billing(svc):
    _, c = svc
    await c.post("/admin/inject/bad_deploy")

    assert (await c.post("/pay", json=make_payload(billing=True))).status_code == 200
    r = await c.post("/pay", json=make_payload(billing=False))
    assert r.status_code == 500

    errors = (await c.get("/admin/logs", params={"level": "ERROR"})).json()
    crash = errors[-1]
    assert crash["error_type"] == "AttributeError"
    assert "NoneType" in crash["error_message"]
    # a real stack, pointing at the real function that crashed
    assert any("validate_payment" in frame for frame in crash["stack"])


@pytest.mark.asyncio
async def test_rollback_fixes_bad_deploy(svc):
    _, c = svc
    await c.post("/admin/inject/bad_deploy")
    codes, _ = await burst(c, 10, billing=False)
    assert set(codes) == {500}

    await c.post("/admin/rollback")
    codes, _ = await burst(c, 10, billing=False)
    assert set(codes) == {200}
    assert (await c.get("/admin/ground_truth")).json()["healthy"] is True


# ------------------------------------------------------ deploy history
@pytest.mark.asyncio
async def test_deploy_history_is_agent_safe_and_ordered(svc):
    _, c = svc
    before = (await c.get("/admin/deploys")).json()
    assert [d["version"] for d in before] == ["v1.4.0", "v1.4.1", "v1.4.2"]

    injected = (await c.post("/admin/inject/bad_deploy")).json()["deployed"]
    after = (await c.get("/admin/deploys")).json()
    assert after[-1]["commit_sha"] == injected["commit_sha"]
    assert after[-1]["author"] == "jsmith"
    # ground truth must never leak into what agents can read
    assert all("bug" not in d and "config" not in d for d in after)
    # and the newest deploy is the most recent in time (what a root-cause heuristic keys on)
    assert after[-1]["deployed_at"] == max(d["deployed_at"] for d in after)


@pytest.mark.asyncio
async def test_rollback_is_recorded_and_stack_bottoms_out(svc):
    _, c = svc
    await c.post("/admin/inject/bad_deploy")  # v1.4.3 live
    r = (await c.post("/admin/rollback")).json()
    assert r["rolled_back"]["version"] == "v1.4.3"
    assert r["now_live"]["version"] == "v1.4.2"

    hist = (await c.get("/admin/deploys")).json()
    assert hist[-1]["kind"] == "rollback" and hist[-1]["version"] == "v1.4.2"
    assert (await c.get("/admin/state")).json()["version"] == "v1.4.2"

    assert (await c.post("/admin/rollback")).status_code == 200  # -> v1.4.1
    assert (await c.post("/admin/rollback")).status_code == 200  # -> v1.4.0
    assert (await c.post("/admin/rollback")).status_code == 409  # nothing left


@pytest.mark.asyncio
async def test_successive_deploys_get_incrementing_versions(svc):
    _, c = svc
    v1 = (await c.post("/admin/inject/bad_deploy")).json()["deployed"]["version"]
    v2 = (await c.post("/admin/inject/slow_query")).json()["deployed"]["version"]
    assert (v1, v2) == ("v1.4.3", "v1.4.4")


# --------------------------------------------------------------- bad_config
@pytest.mark.asyncio
async def test_bad_config_starves_the_pool_under_concurrency():
    app, c = build(latency_scale=1.0)
    await c.post("/admin/inject/bad_config")
    codes, rs = await burst(c, 20)
    assert codes.count(504) >= 5, codes
    assert codes.count(200) >= 1, codes
    msg = next(r.json()["message"] for r in rs if r.status_code == 504)
    assert "pool_size=1" in msg
    assert (await c.get("/admin/state")).json()["db_pool"]["size"] == 1
    await c.aclose()


@pytest.mark.asyncio
async def test_healthy_pool_handles_the_same_concurrency_and_rollback_restores_it():
    app, c = build(latency_scale=1.0)
    codes, _ = await burst(c, 20)
    assert 504 not in codes

    await c.post("/admin/inject/bad_config")
    await c.post("/admin/rollback")
    assert (await c.get("/admin/state")).json()["db_pool"]["size"] == 10
    codes, _ = await burst(c, 20)
    assert 504 not in codes
    await c.aclose()


# --------------------------------------------------------------- slow_query
@pytest.mark.asyncio
async def test_slow_query_affects_refund_only_and_logs_it():
    app, c = build(latency_scale=0.05)
    await c.post("/admin/inject/slow_query")
    await c.post("/pay", json=make_payload())
    await c.post("/refund", json=REFUND)

    logs = (await c.get("/admin/logs", params={"contains": "slow query"})).json()
    assert len(logs) == 1 and logs[0]["endpoint"] == "/refund"
    assert "seq scan" in logs[0]["message"]

    by_ep = (await c.get("/stats")).json()["by_endpoint"]
    assert by_ep["/refund"]["latency_ms"]["max"] > 5 * by_ep["/pay"]["latency_ms"]["max"]
    await c.aclose()


# -------------------------------------------------------------- memory_leak
@pytest.mark.asyncio
async def test_memory_leak_retains_memory_and_rollback_frees_it(svc):
    service, c = svc
    await c.post("/admin/inject/memory_leak")
    await burst(c, 60)

    assert (await c.get("/admin/state")).json()["cache_retained_mb"] > 5
    metrics = (await c.get("/metrics")).text
    line = next(l for l in metrics.splitlines() if l.startswith("payments_cache_retained_bytes "))
    assert float(line.split()[1]) > 5 * 1024 * 1024

    gc = (await c.get("/admin/logs", params={"contains": "GC pause"})).json()
    assert gc and "merchant_cache" in gc[0]["message"]

    await c.post("/admin/rollback")  # a deploy restarts the process -> memory freed
    assert (await c.get("/admin/state")).json()["cache_retained_mb"] == 0


@pytest.mark.asyncio
async def test_memory_leak_is_capped_so_it_can_never_oom_a_host():
    app, c = build(leak_cap_entries=10, leak_entry_kb=8)
    await c.post("/admin/inject/memory_leak")
    await burst(c, 50)
    assert len(app.state.service.leak_cache) == 10
    await c.aclose()


@pytest.mark.asyncio
async def test_leak_shows_up_as_a_new_slow_span_in_traces():
    app, c = build(latency_scale=0.02)
    await c.post("/admin/inject/memory_leak")
    await burst(c, 40)
    traces = (await c.get("/admin/traces", params={"limit": 40})).json()
    ops = {s["operation"] for t in traces for s in t["spans"]}
    assert "merchant_cache.lookup" in ops
    await c.aclose()


# --------------------------------------------------------- downstream outage
@pytest.mark.asyncio
async def test_downstream_outage_needs_no_deploy_and_clears_cleanly(svc):
    _, c = svc
    deploys_before = (await c.get("/admin/deploys")).json()

    await c.post("/admin/inject/downstream_outage")
    codes, rs = await burst(c, 30)
    assert codes.count(502) >= 20
    assert any("bank-gateway" in r.json()["message"] for r in rs if r.status_code == 502)
    # nothing changed in the deploy history: there is no "culprit commit"
    assert (await c.get("/admin/deploys")).json() == deploys_before
    truth = (await c.get("/admin/ground_truth")).json()
    assert truth["external_faults"] == ["downstream_outage"] and truth["culprit_commit"] is None

    await c.put("/admin/faults/downstream_outage", json={"enabled": False})
    codes, _ = await burst(c, 20)
    assert set(codes) == {200}


# -------------------------------------------------------------- telemetry
@pytest.mark.asyncio
async def test_stats_error_rate_and_percentiles(svc):
    _, c = svc
    await c.post("/admin/inject/bad_deploy")
    await burst(c, 10, billing=False)
    await burst(c, 10, billing=True)
    o = (await c.get("/stats")).json()["overall"]
    assert o["requests"] == 20 and o["errors_5xx"] == 10 and o["error_rate"] == 0.5
    assert set(o["latency_ms"]) == {"p50", "p95", "p99", "max"}


@pytest.mark.asyncio
async def test_prometheus_metrics_and_single_build_info_label(svc):
    _, c = svc
    await burst(c, 5)
    text = (await c.get("/metrics")).text
    assert 'payments_http_requests_total{endpoint="/pay",status="200"} 5.0' in text
    assert "payments_http_request_duration_seconds_bucket" in text
    assert "payments_db_pool_size 10.0" in text

    await c.post("/admin/inject/bad_deploy")
    text = (await c.get("/metrics")).text
    builds = [l for l in text.splitlines() if l.startswith("payments_build_info{")]
    assert len(builds) == 1 and 'version="v1.4.3"' in builds[0]


@pytest.mark.asyncio
async def test_traces_have_expected_spans_and_flag_the_crash(svc):
    _, c = svc
    await c.post("/pay", json=make_payload())
    ok = (await c.get("/admin/traces")).json()[-1]
    assert [s["operation"] for s in ok["spans"]] == ["validate", "db.query", "bank.charge"]

    await c.post("/admin/inject/bad_deploy")
    await c.post("/pay", json=make_payload(billing=False))
    bad = (await c.get("/admin/traces", params={"errors_only": True})).json()
    assert len(bad) == 1
    assert bad[0]["spans"][0]["operation"] == "validate" and bad[0]["spans"][0]["error"] is True


@pytest.mark.asyncio
async def test_log_queries_filter_by_level_time_and_text(svc):
    _, c = svc
    await c.post("/pay", json=make_payload())
    await c.post("/pay", json={"currency": "USD"})  # 400 -> WARNING
    warns = (await c.get("/admin/logs", params={"level": "WARNING"})).json()
    assert warns and all(l["level"] in ("WARNING", "ERROR") for l in warns)

    cutoff = warns[-1]["ts"] + 0.001
    assert (await c.get("/admin/logs", params={"since": cutoff, "level": "WARNING"})).json() == []
    assert (await c.get("/admin/logs", params={"contains": "amount must be"})).json()
    assert (await c.get("/admin/logs", params={"window": 60})).json()


@pytest.mark.asyncio
async def test_deploy_events_are_logged_like_a_real_service(svc):
    _, c = svc
    d = (await c.post("/admin/inject/bad_deploy")).json()["deployed"]
    logs = (await c.get("/admin/logs", params={"contains": d["commit_sha"]})).json()
    msgs = " ".join(l["message"] for l in logs)
    assert "deploy v1.4.3" in msgs and "jsmith" in msgs


# ------------------------------------------------------------ admin / misc
@pytest.mark.asyncio
async def test_admin_token_protects_admin_but_not_public_endpoints():
    app, c = build(admin_token="s3cret")
    assert (await c.get("/admin/state")).status_code == 401
    assert (await c.get("/admin/state", headers={"X-Admin-Token": "nope"})).status_code == 401
    assert (await c.get("/admin/state", headers={"X-Admin-Token": "s3cret"})).status_code == 200
    assert (await c.post("/admin/inject/bad_deploy")).status_code == 401
    assert (await c.get("/health")).status_code == 200
    assert (await c.post("/pay", json=make_payload())).status_code == 200
    assert (await c.get("/metrics")).status_code == 200
    await c.aclose()


@pytest.mark.asyncio
async def test_reset_restores_a_fully_healthy_baseline(svc):
    service, c = svc
    await c.post("/admin/inject/memory_leak")
    await c.post("/admin/inject/downstream_outage")
    await burst(c, 20)
    await c.post("/admin/reset")

    assert (await c.get("/admin/ground_truth")).json()["healthy"] is True
    assert len((await c.get("/admin/deploys")).json()) == 3
    assert (await c.get("/admin/state")).json()["cache_retained_mb"] == 0
    codes, _ = await burst(c, 10, billing=False)
    assert set(codes) == {200}


@pytest.mark.asyncio
async def test_ground_truth_names_the_culprit_commit_then_clears(svc):
    _, c = svc
    d = (await c.post("/admin/inject/bad_config")).json()["deployed"]
    truth = (await c.get("/admin/ground_truth")).json()
    assert truth["healthy"] is False and truth["culprit_commit"] == d["commit_sha"]
    assert truth["config"]["db_pool_size"] == 1

    await c.post("/admin/rollback")
    truth = (await c.get("/admin/ground_truth")).json()
    assert truth["healthy"] is True and truth["culprit_commit"] is None


@pytest.mark.asyncio
async def test_bad_admin_input_is_rejected_cleanly(svc):
    _, c = svc
    assert (await c.post("/admin/inject/nope")).status_code == 404
    assert (await c.put("/admin/faults/nope", json={"enabled": True})).status_code == 404
    r = await c.post("/admin/deploy", json={"author": "x", "message": "y", "bug": "nope"})
    assert r.status_code == 422
    r = await c.post("/admin/deploy", json={"author": "x", "message": "y", "bug": "null_pointer"})
    assert r.status_code == 200 and r.json()["version"] == "v1.4.3"


@pytest.mark.asyncio
async def test_scenarios_endpoint_lists_all_five(svc):
    _, c = svc
    got = (await c.get("/admin/scenarios")).json()
    assert set(got) == {"bad_deploy", "memory_leak", "bad_config", "slow_query", "downstream_outage"}


# -------------------------------------------------------- traffic generator
@pytest.mark.asyncio
async def test_traffic_generator_mix_matches_configuration():
    gen = TrafficGenerator(client=None, seed=3)
    reqs = [gen._next_request() for _ in range(4000)]
    pays = [b for ep, b in reqs if ep == "/pay"]
    assert 0.80 < len(pays) / len(reqs) < 0.90
    with_billing = sum("billing" in b for b in pays) / len(pays)
    assert 0.35 < with_billing < 0.45


@pytest.mark.asyncio
async def test_traffic_generator_drives_the_service():
    app, c = build()
    gen = TrafficGenerator(c, rps=300, seed=2)
    await gen.run(duration=0.5)
    assert gen.sent > 30
    assert gen.status_counts["200"] > 0
    assert (await c.get("/stats")).json()["overall"]["requests"] >= gen.sent * 0.8
    await c.aclose()


@pytest.mark.asyncio
async def test_built_in_traffic_starts_and_stops_with_the_app():
    app = create_app(Settings(latency_scale=0.0, enable_traffic=True, traffic_rps=300, seed=4))
    app.state.service.telemetry.silence_stdout()
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.5)
        assert app.state.service.telemetry.stats(window=10)["overall"]["requests"] > 20
    assert app.state.traffic.sent > 20
