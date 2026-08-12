"""
Shared retry policy for anything that talks to an external system
(observability APIs, the LLM). Centralized here instead of hand-rolled
in each agent so every "flaky call" in the pipeline behaves consistently
and the policy is a single place to tune.
"""

from __future__ import annotations

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)
import logging

logger = logging.getLogger("sre_agent_crew")
logging.basicConfig(level=logging.INFO, format="%(message)s")


class TransientAPIError(Exception):
    """Raised by the fake data store (or a real API client) for a
    retryable failure -- timeout, 429, 503, connection reset, etc."""


def with_retries(max_attempts: int = 3):
    """
    3 attempts, exponential backoff starting at 0.5s, only retries
    TransientAPIError (a bad request or auth error should fail fast,
    not retry -- retrying those just wastes time and hides real bugs).
    """
    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        retry=retry_if_exception_type(TransientAPIError),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )