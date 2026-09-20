"""
FastAPI app for the fake payments service.

Public endpoints (what a real service exposes):
    POST /pay  POST /refund   GET /health   GET /metrics   GET /stats

Admin endpoints (what the crew's data sources and the demo dashboard use).
Protected by X-Admin-Token when ADMIN_TOKEN is set:
    GET  /admin/state /deploys /logs /traces /scenarios /ground_truth
    POST /admin/deploy /rollback /inject/{scenario} /reset
    PUT  /admin/faults/{name}

`/admin/ground_truth` exists only for tests and the demo UI. The agents'
data sources must never call it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST
from pydantic import BaseModel, Field

from .deploys import BUG_MEMORY_LEAK, BUG_NULL_POINTER, BUG_SLOW_QUERY, EXTERNAL_FAULTS, SCENARIOS
from .service import PaymentsService
from .settings import Settings
from .traffic_generator import TrafficGenerator

_KNOWN_BUGS = {BUG_NULL_POINTER, BUG_MEMORY_LEAK, BUG_SLOW_QUERY}


class DeployRequest(BaseModel):
    author: str
    message: str
    kind: str = Field("code", pattern="^(code|config)$")
    bug: Optional[str] = None
    config: Optional[dict] = None
    version: Optional[str] = None
    commit_sha: Optional[str] = None


class FaultRequest(BaseModel):
    enabled: bool


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings()
    svc = PaymentsService(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task = client = None
        if settings.enable_traffic:
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://payments.internal"
            )
            gen = TrafficGenerator(client, rps=settings.traffic_rps, seed=settings.seed)
            app.state.traffic = gen
            task = asyncio.create_task(gen.run())
            svc.telemetry.log("INFO", f"built-in traffic generator started at {settings.traffic_rps} rps")
        svc.telemetry.log("INFO", f"payments-api starting on {svc.releases.current.version}")
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if client:
                await client.aclose()

    app = FastAPI(title="Fake Payments Service", version="1.0", lifespan=lifespan)
    app.state.service = svc

    # ------------------------------------------------------------ public
    async def _json_body(request: Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            data = None
        return data if isinstance(data, dict) else {}

    @app.post("/pay")
    async def pay(request: Request):
        status, body = await svc.process("/pay", await _json_body(request))
        return JSONResponse(body, status_code=status)

    @app.post("/refund")
    async def refund(request: Request):
        status, body = await svc.process("/refund", await _json_body(request))
        return JSONResponse(body, status_code=status)

    @app.get("/health")
    async def health():
        recent = svc.telemetry.stats(window=30)["overall"]
        degraded = recent["requests"] >= 10 and recent["error_rate"] > 0.2
        return {
            "status": "degraded" if degraded else "ok",
            "version": svc.releases.current.version,
            "error_rate_30s": recent["error_rate"],
        }

    @app.get("/metrics")
    async def metrics():
        cur = svc.releases.current
        payload = svc.telemetry.render_metrics(
            pool_size=svc.pool.size, pool_in_use=svc.pool.in_use,
            leak_bytes=svc._cache_bytes(), version=cur.version, commit=cur.commit_sha,
        )
        return Response(payload, media_type=CONTENT_TYPE_LATEST)

    @app.get("/stats")
    async def stats(window: float = Query(60, gt=0, le=3600)):
        return svc.telemetry.stats(window=window)

    # ------------------------------------------------------------ admin
    def require_admin(x_admin_token: Optional[str] = Header(default=None)) -> None:
        if settings.admin_token and not hmac.compare_digest(x_admin_token or "", settings.admin_token):
            raise HTTPException(status_code=401, detail="invalid or missing X-Admin-Token")

    admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

    @admin.get("/state")
    async def state():
        return svc.state()

    @admin.get("/deploys")
    async def deploys(since: Optional[float] = None):
        """Agent-facing deploy history. Ground-truth bug labels are stripped."""
        return [d.public() for d in svc.releases.history if since is None or d.deployed_at >= since]

    @admin.get("/logs")
    async def logs(
        since: Optional[float] = None,
        until: Optional[float] = None,
        window: Optional[float] = Query(None, gt=0, description="last N seconds"),
        level: str = "DEBUG",
        contains: Optional[str] = None,
        limit: int = Query(500, ge=1, le=5000),
    ):
        if window is not None and since is None:
            since = time.time() - window
        return svc.telemetry.query_logs(since, until, level, limit, contains)

    @admin.get("/traces")
    async def traces(
        since: Optional[float] = None,
        window: Optional[float] = Query(None, gt=0),
        errors_only: bool = False,
        limit: int = Query(100, ge=1, le=500),
    ):
        if window is not None and since is None:
            since = time.time() - window
        return svc.telemetry.query_traces(since, errors_only, limit)

    @admin.get("/scenarios")
    async def scenarios():
        return {k: v["description"] for k, v in SCENARIOS.items()}

    @admin.get("/ground_truth")
    async def ground_truth():
        return svc.ground_truth()

    @admin.post("/deploy")
    async def deploy(req: DeployRequest):
        if req.bug and req.bug not in _KNOWN_BUGS:
            raise HTTPException(422, f"unknown bug '{req.bug}'. choices: {sorted(_KNOWN_BUGS)}")
        return svc.deploy(**req.model_dump()).public()

    @admin.post("/rollback")
    async def rollback():
        try:
            bad, live = svc.rollback()
        except ValueError as e:
            raise HTTPException(409, str(e))
        return {"rolled_back": bad.public(), "now_live": live.public()}

    @admin.post("/inject/{scenario}")
    async def inject(scenario: str):
        if scenario not in SCENARIOS:
            raise HTTPException(404, f"unknown scenario '{scenario}'. choices: {sorted(SCENARIOS)}")
        return svc.inject(scenario)

    @admin.put("/faults/{name}")
    async def set_fault(name: str, req: FaultRequest):
        if name not in EXTERNAL_FAULTS:
            raise HTTPException(404, f"unknown fault '{name}'. choices: {sorted(EXTERNAL_FAULTS)}")
        svc.set_fault(name, req.enabled)
        return {"fault": name, "enabled": req.enabled}

    @admin.post("/reset")
    async def reset():
        svc.reset()
        return {"status": "reset", "version": svc.releases.current.version}

    app.include_router(admin)
    return app
