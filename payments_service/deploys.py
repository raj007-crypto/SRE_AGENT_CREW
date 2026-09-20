"""
Deploy / release management for the fake payments service.

The service keeps two things:
  * `history`  -- an append-only audit log of every deploy, config change and
                  rollback (this is what agents read to find "what changed?").
  * `stack`    -- the live releases, newest last. A rollback pops the stack.

Each release optionally carries a hidden `bug`. That field is GROUND TRUTH for
tests and the demo dashboard; it is deliberately never included in the
agent-facing history (see `Deploy.public()`), so agents must work out the
cause from logs, metrics and traces -- not read it off the record.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

# Bugs that can ride along inside a release.
BUG_NULL_POINTER = "null_pointer"    # payment validation crashes on missing billing info
BUG_MEMORY_LEAK = "memory_leak"      # a cache that never evicts
BUG_SLOW_QUERY = "slow_query"        # /refund does a sequential scan
# (bad_config is not a code bug: it is a config change, db_pool_size -> 1)

# Faults NOT caused by any deploy (the outside world breaking).
FAULT_DOWNSTREAM_OUTAGE = "downstream_outage"
EXTERNAL_FAULTS = {FAULT_DOWNSTREAM_OUTAGE}

HEALTHY_CONFIG = {"db_pool_size": 10}


@dataclass
class Deploy:
    version: str
    commit_sha: str
    author: str
    message: str
    kind: str = "code"                 # "code" | "config" | "rollback"
    deployed_at: float = field(default_factory=time.time)
    config: dict = field(default_factory=lambda: dict(HEALTHY_CONFIG))
    bug: Optional[str] = None          # ground truth -- never exposed to agents

    def public(self) -> dict:
        """What an agent's DeploySource is allowed to see."""
        return {
            "version": self.version,
            "commit_sha": self.commit_sha,
            "author": self.author,
            "message": self.message,
            "kind": self.kind,
            "deployed_at": self.deployed_at,
        }


# Canned "bad change" scenarios used by the demo's "Break it" buttons.
# `deploy` is None for faults that are not caused by a deploy.
SCENARIOS: dict[str, dict] = {
    "bad_deploy": {
        "description": "A refactor of payment validation introduces a null-pointer bug.",
        "deploy": {
            "author": "jsmith",
            "message": "Add billing address verification to payment validation",
            "kind": "code",
            "bug": BUG_NULL_POINTER,
        },
    },
    "memory_leak": {
        "description": "A new in-memory cache holds references forever, causing GC pressure.",
        "deploy": {
            "author": "rpatel",
            "message": "Add in-memory cache for merchant lookups",
            "kind": "code",
            "bug": BUG_MEMORY_LEAK,
        },
    },
    "bad_config": {
        "description": "A cost-saving config change drops the DB connection pool far too low.",
        "deploy": {
            "author": "tchen",
            "message": "Reduce db_pool_size from 10 to 1 to lower connection costs",
            "kind": "config",
            "config": {"db_pool_size": 1},
        },
    },
    "slow_query": {
        "description": "A refund lookup ships without its index, forcing sequential scans.",
        "deploy": {
            "author": "mlopez",
            "message": "Look up refunds by customer email (index migration pending)",
            "kind": "code",
            "bug": BUG_SLOW_QUERY,
        },
    },
    "downstream_outage": {
        "description": "The upstream bank gateway goes down. No deploy is to blame.",
        "deploy": None,
        "fault": FAULT_DOWNSTREAM_OUTAGE,
    },
}


def _sha() -> str:
    return secrets.token_hex(4)[:7]


def baseline_history(now: Optional[float] = None) -> list[Deploy]:
    """Three ordinary, healthy deploys in the recent past."""
    now = now or time.time()
    day = 86400
    return [
        Deploy("v1.4.0", "9f3a1c2", "asingh", "Add idempotency keys to /pay",
               deployed_at=now - 3 * day),
        Deploy("v1.4.1", "b7d40e9", "kwong", "Improve decline-reason logging",
               deployed_at=now - 2 * day),
        Deploy("v1.4.2", "4c8e2fa", "asingh", "Bump http client timeouts",
               deployed_at=now - 1 * day),
    ]


class ReleaseManager:
    def __init__(self) -> None:
        self.history: list[Deploy] = []
        self.stack: list[Deploy] = []
        self.reset()

    # ---- state --------------------------------------------------------
    @property
    def current(self) -> Deploy:
        return self.stack[-1]

    def reset(self) -> None:
        base = baseline_history()
        self.history = list(base)
        self.stack = list(base)

    # ---- actions ------------------------------------------------------
    def _next_version(self) -> str:
        latest = max(
            (d.version for d in self.history if d.kind != "rollback"),
            key=lambda v: tuple(int(p) for p in v.lstrip("v").split(".")),
        )
        major, minor, patch = (int(p) for p in latest.lstrip("v").split("."))
        return f"v{major}.{minor}.{patch + 1}"

    def deploy(
        self,
        *,
        author: str,
        message: str,
        kind: str = "code",
        bug: Optional[str] = None,
        config: Optional[dict] = None,
        version: Optional[str] = None,
        commit_sha: Optional[str] = None,
    ) -> Deploy:
        # A new release inherits the running config unless it changes it.
        new_config = dict(self.current.config)
        if config:
            new_config.update(config)
        d = Deploy(
            version=version or self._next_version(),
            commit_sha=commit_sha or _sha(),
            author=author,
            message=message,
            kind=kind,
            config=new_config,
            bug=bug,
        )
        self.history.append(d)
        self.stack.append(d)
        return d

    def rollback(self) -> tuple[Deploy, Deploy]:
        """Revert to the previous live release. Returns (rolled_back, now_live)."""
        if len(self.stack) < 2:
            raise ValueError("nothing to roll back to")
        bad = self.stack.pop()
        target = self.stack[-1]
        self.history.append(
            Deploy(
                version=target.version,
                commit_sha=target.commit_sha,
                author="rollback-bot",
                message=f"Rollback of {bad.version} ({bad.commit_sha})",
                kind="rollback",
                config=dict(target.config),
                bug=target.bug,
            )
        )
        return bad, target
