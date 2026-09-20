"""
Synthetic traffic for the payments service, so metrics always have a live baseline.

Runs two ways:
  * inside the service process (ENABLE_TRAFFIC=1) via an in-memory ASGI transport
  * as a standalone CLI against any URL:
        python -m payments_service.traffic_generator --url http://localhost:8001 --rps 15

Arrivals are Poisson (bursty, like real users) rather than a metronome, which
matters: concurrency spikes are what expose the undersized-connection-pool fault.
"""

from __future__ import annotations

import argparse
import asyncio
import random
from collections import Counter
from typing import Optional

import httpx

_CURRENCIES = ["USD", "EUR", "GBP", "INR"]


class TrafficGenerator:
    def __init__(
        self,
        client: httpx.AsyncClient,
        rps: float = 15.0,
        seed: Optional[int] = None,
        max_in_flight: int = 200,
        pay_ratio: float = 0.85,
        billing_ratio: float = 0.4,
    ) -> None:
        self.client = client
        self.rps = rps
        self.rng = random.Random(seed)
        self.max_in_flight = max_in_flight
        self.pay_ratio = pay_ratio
        # Fraction of /pay requests that include a billing block. The null-pointer
        # bug only bites requests that omit it (~60% at the default).
        self.billing_ratio = billing_ratio
        self.status_counts: Counter[str] = Counter()
        self.sent = 0
        self.dropped = 0
        self._in_flight = 0

    def _next_request(self) -> tuple[str, dict]:
        r = self.rng
        if r.random() < self.pay_ratio:
            body = {
                "amount": round(r.uniform(5, 500), 2),
                "currency": r.choice(_CURRENCIES),
                "customer_id": f"cus_{r.randrange(10_000):05d}",
                "card_token": f"tok_{r.getrandbits(32):08x}",
            }
            if r.random() < self.billing_ratio:
                body["billing"] = {"zip": f"{r.randrange(10_000, 99_999)}", "country": "US"}
            return "/pay", body
        return "/refund", {
            "transaction_id": f"txn_{r.getrandbits(40):010x}",
            "customer_email": f"user{r.randrange(5000)}@example.com",
        }

    async def _one(self, endpoint: str, body: dict) -> None:
        self._in_flight += 1
        try:
            resp = await self.client.post(endpoint, json=body, timeout=15)
            self.status_counts[str(resp.status_code)] += 1
        except Exception:
            self.status_counts["client_error"] += 1
        finally:
            self._in_flight -= 1

    async def run(self, duration: Optional[float] = None) -> None:
        loop = asyncio.get_running_loop()
        end = loop.time() + duration if duration else None
        tasks: set[asyncio.Task] = set()
        try:
            while end is None or loop.time() < end:
                await asyncio.sleep(self.rng.expovariate(self.rps))
                if self._in_flight >= self.max_in_flight:
                    self.dropped += 1
                    continue
                endpoint, body = self._next_request()
                self.sent += 1
                t = asyncio.create_task(self._one(endpoint, body))
                tasks.add(t)
                t.add_done_callback(tasks.discard)
        finally:
            for t in list(tasks):
                t.cancel()
            if duration and tasks:
                await asyncio.gather(*tasks, return_exceptions=True)


async def _cli(url: str, rps: float, duration: Optional[float], seed: Optional[int]) -> None:
    async with httpx.AsyncClient(base_url=url) as client:
        gen = TrafficGenerator(client, rps=rps, seed=seed)
        task = asyncio.create_task(gen.run(duration))
        try:
            while not task.done():
                await asyncio.sleep(5)
                print(f"[traffic] sent={gen.sent} in_flight={gen._in_flight} "
                      f"status={dict(gen.status_counts)}", flush=True)
        except asyncio.CancelledError:
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Send synthetic traffic to the payments service")
    p.add_argument("--url", default="http://localhost:8001")
    p.add_argument("--rps", type=float, default=15.0)
    p.add_argument("--duration", type=float, default=None, help="seconds (default: run forever)")
    p.add_argument("--seed", type=int, default=None)
    args = p.parse_args()
    try:
        asyncio.run(_cli(args.url, args.rps, args.duration, args.seed))
    except KeyboardInterrupt:
        pass
