"""Runtime configuration for the payments service, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # Multiplies every simulated delay. 1.0 = realistic timings, 0.0 = instant
    # (used by most unit tests so they don't sleep).
    latency_scale: float = 1.0

    # If set, every /admin/* call must send this in the X-Admin-Token header.
    # Unset (the default) means open, which is fine for local development.
    admin_token: str | None = None

    # Run a built-in traffic generator inside the app process, so a single
    # deployed container always has a live baseline of traffic.
    enable_traffic: bool = False
    traffic_rps: float = 15.0

    # Optional: also write JSON logs to this file (rotating).
    log_file: str | None = None

    # Seed for the service's random behaviour (None = non-deterministic).
    seed: int | None = None

    # Baseline (healthy) behaviour.
    healthy_pool_size: int = 10
    card_decline_rate: float = 0.03      # 4xx business errors, NOT counted as failures
    baseline_error_rate: float = 0.003   # rare transient 5xx noise

    # Memory-leak fault tuning.
    leak_entry_kb: int = 256             # memory retained per leaking request
    leak_cap_entries: int = 600          # hard cap so the demo can never OOM a host (~150 MB)
    leak_ms_per_entry: float = 6.0       # GC-pressure latency added per retained entry
    leak_max_added_ms: float = 3000.0

    # Ring-buffer sizes (how much history the service keeps in memory).
    max_logs: int = 5000
    max_requests: int = 20000
    max_traces: int = 500

    @classmethod
    def from_env(cls) -> "Settings":
        seed = os.getenv("SEED")
        return cls(
            latency_scale=float(os.getenv("LATENCY_SCALE", "1.0")),
            admin_token=os.getenv("ADMIN_TOKEN") or None,
            enable_traffic=_env_bool("ENABLE_TRAFFIC", False),
            traffic_rps=float(os.getenv("TRAFFIC_RPS", "15")),
            log_file=os.getenv("LOG_FILE") or None,
            seed=int(seed) if seed else None,
        )
