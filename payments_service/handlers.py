"""
Business logic for the fake payments service.

The faults here are REAL code paths, not fabricated log lines. For example the
null-pointer bug is an actual `None.get(...)` that raises an actual
AttributeError with an actual traceback, and the bad-config fault is an
actual undersized connection pool that actually starves concurrent requests.
"""

from __future__ import annotations

import asyncio

from .deploys import BUG_NULL_POINTER


# --------------------------------------------------------------------------
# Errors, each mapped to an HTTP status by the service
# --------------------------------------------------------------------------
class ValidationFailed(Exception):
    status = 400


class CardDeclined(Exception):
    status = 402


class PoolTimeout(Exception):
    """Timed out waiting for a DB connection (bad_config makes this common)."""
    status = 504


class BankUnavailable(Exception):
    status = 502


class TransientUpstream(Exception):
    """Rare, random blip from an upstream dependency (baseline noise)."""
    status = 502


# --------------------------------------------------------------------------
# Validation -- home of the null-pointer bug
# --------------------------------------------------------------------------
def validate_payment(payload: dict, bug: str | None) -> None:
    amount = payload.get("amount")
    if not isinstance(amount, (int, float)) or amount <= 0:
        raise ValidationFailed("amount must be a positive number")
    if not payload.get("customer_id"):
        raise ValidationFailed("customer_id is required")
    if not payload.get("card_token"):
        raise ValidationFailed("card_token is required")

    if bug == BUG_NULL_POINTER:
        # Newly shipped "billing address verification". It assumes `billing`
        # is always present -- but clients that omit it now crash the handler.
        zip_code = payload.get("billing").get("zip")
        if len(zip_code) < 3:
            raise ValidationFailed("invalid billing zip")


def validate_refund(payload: dict) -> None:
    if not payload.get("transaction_id"):
        raise ValidationFailed("transaction_id is required")
    if not payload.get("customer_email"):
        raise ValidationFailed("customer_email is required")


# --------------------------------------------------------------------------
# A real (tiny) connection pool. bad_config sets its size to 1.
# --------------------------------------------------------------------------
class DbPool:
    def __init__(self, size: int, acquire_timeout: float) -> None:
        self.size = size
        self.acquire_timeout = acquire_timeout
        self._sem = asyncio.Semaphore(size)
        self.in_use = 0

    async def acquire(self) -> None:
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self.acquire_timeout)
        except asyncio.TimeoutError:
            raise PoolTimeout(
                f"timed out waiting for a database connection "
                f"(pool_size={self.size}, in_use={self.in_use}, "
                f"waited={int(self.acquire_timeout * 1000)}ms)"
            ) from None
        self.in_use += 1

    def release(self) -> None:
        self.in_use -= 1
        self._sem.release()
