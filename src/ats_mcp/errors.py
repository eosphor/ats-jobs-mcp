"""Categorized error model for the ATS fetchers.

Every failure is mapped to ONE stable category so that:
  - logs and the run summary are greppable,
  - the retry decision is deterministic (``retryable`` flag),
  - the caller can decide whether to keep going or abort.

Categories are derived from the HTTP status first, then from transport/parse
failures. Keep the category strings stable — dashboards and
alerting depend on them.

Do not add retries here; retry policy lives in ``http_client.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Type


class ActorError(Exception):
    """Base class for every actor error.

    ``category``  stable, greppable tag (used as the counter key)
    ``retryable`` whether the HTTP layer is allowed to retry this failure
    """

    category: str = "actor_error"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        url: Optional[str] = None,
        status: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.url = url
        self.status = status

    def as_dict(self) -> dict:
        return {
            "category": self.category,
            "message": self.message,
            "url": self.url,
            "status": self.status,
            "retryable": self.retryable,
        }


class InputError(ActorError):
    """Bad user input. Fail fast: no retry, no PPE charge."""

    category = "input_error"
    retryable = False


class AuthError(ActorError):
    """401/403 — credentials missing or invalid. Not retryable."""

    category = "auth_error"
    retryable = False


class NotFoundError(ActorError):
    """404/410 — resource is gone. Not retryable."""

    category = "not_found"
    retryable = False


class RateLimitError(ActorError):
    """429 — retryable, honour ``Retry-After`` when present."""

    category = "rate_limited"
    retryable = True

    def __init__(
        self,
        message: str,
        *,
        retry_after: Optional[float] = None,
        **kw,
    ) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after  # seconds, or None


class ClientError(ActorError):
    """Any other 4xx — the request itself is wrong. Not retryable."""

    category = "client_error"
    retryable = False


class ServerError(ActorError):
    """5xx — transient upstream failure. Retryable."""

    category = "server_error"
    retryable = True


class NetworkError(ActorError):
    """Transport failure: timeout, DNS, TLS, connection reset. Retryable."""

    category = "network_error"
    retryable = True


class SilentFailureError(ActorError):
    """HTTP 200, but the payload is empty or broken — the dangerous case.

    A 200 with an empty body, a login wall, a rate-limit HTML page, or a JSON
    schema that no longer matches all look like "success" to a naive client.
    We treat them as failures so the actor never emits junk as if it were data.

    Not retryable by default: the upstream answered successfully, just
    uselessly. If a specific target flaps, raise the retry budget upstream
    (``treat_silent_as_retryable``) rather than looping here.
    """

    category = "silent_failure"
    retryable = False


def classify_status(status: int) -> Type[ActorError]:
    """Map an HTTP status to the error class the HTTP layer should raise."""
    if status in (401, 403):
        return AuthError
    if status in (404, 410):
        return NotFoundError
    if status == 429:
        return RateLimitError
    if 400 <= status < 500:
        return ClientError
    if 500 <= status < 600:
        return ServerError
    # 3xx reaching us (redirects disabled) and 1xx: treat as client-side.
    return ClientError


@dataclass
class ErrorCounters:
    """Run-scoped error tally. Populates the run summary in the KVS.

    Not thread-safe by design — the template drives one worker. If you add
    concurrency, guard ``record`` with your scheduler's lock or switch to
    per-task counters and merge at the end.
    """

    counts: dict = field(default_factory=dict)
    samples: dict = field(default_factory=dict)

    def record(self, exc: ActorError) -> None:
        cat = exc.category
        self.counts[cat] = self.counts.get(cat, 0) + 1
        # keep the first sample per category: cheap, no unbounded growth
        self.samples.setdefault(cat, str(exc.message)[:300])

    def total(self) -> int:
        return sum(self.counts.values())

    def as_dict(self) -> dict:
        return {
            "total": self.total(),
            "by_category": dict(
                sorted(self.counts.items(), key=lambda kv: (-kv[1], kv[0]))
            ),
            "samples": dict(self.samples),
        }
