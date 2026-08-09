"""Retrying an APIC *status*, as opposed to a network error.

The transport has always retried transport-level failures — a refused
connection, an exhausted pool.  It never retried an HTTP status, so a 503 from
a controller that was merely busy ended a staged push mid-flight.

Two rules shape what is retryable here, and they are not the same for reads and
writes.

**A read may be replayed freely.** It has no effect on the fabric, so any status
that means "not now" is worth another attempt: ``502``, ``503``, ``504``.

**A write may only be replayed when it provably never took effect.** That is a
much smaller set — ``503`` alone.  A gateway that answers ``502`` or ``504`` has
already forwarded the request, and the APIC may well have applied it; replaying
would double-apply, or 404 against an object the first attempt created.  This
mirrors the existing rule for network errors, where writes retry on connection
failures but never on read timeouts.

``429`` is deliberately absent: nothing observed on a 6.0(9c) controller emits
it, and a retry set is a claim about a controller's behaviour, not a wish.
"""

from __future__ import annotations

from email.utils import parsedate_to_datetime

import httpx

# Statuses worth another attempt, by request kind.  Disjoint on purpose — see
# the module docstring for why a write cannot inherit the read set.
READ_RETRY_STATUSES: frozenset[int] = frozenset({502, 503, 504})
WRITE_RETRY_STATUSES: frozenset[int] = frozenset({503})


class RetryableStatus(Exception):
    """Internal signal: a response whose status is worth another attempt.

    Raised inside the retry loop so ``stamina`` counts the attempt and applies
    its backoff, then caught on the final attempt so the caller receives the
    response rather than an exception it cannot inspect.

    Attributes:
        response: The response that triggered the retry, carried so the last
            attempt can be returned unchanged.
        retry_after: Seconds the controller asked the caller to wait, already
            clamped, or ``None`` when it asked for nothing.
    """

    def __init__(self, response: httpx.Response, retry_after: float | None) -> None:
        self.response = response
        self.retry_after = retry_after
        super().__init__(f"HTTP {response.status_code} is retryable")


def retry_after_seconds(
    response: httpx.Response, *, ceiling: float, now: float = 0.0
) -> float | None:
    """Read ``Retry-After`` from *response*, clamped to *ceiling*.

    The header comes in two spellings, and a controller may use either: a
    number of seconds (``Retry-After: 5``) or an HTTP date (``Retry-After: Wed,
    21 Oct 2026 07:28:00 GMT``).  Both are accepted.

    A malformed value is treated as absent rather than fatal: a header the SDK
    cannot parse must not turn a retryable response into a crash inside the
    retry loop.

    Args:
        response: The response to read the header from.
        ceiling: Upper bound in seconds.  A controller asking for an hour is
            clamped to this, so one busy answer cannot park a push.
        now: Epoch seconds to measure an HTTP-date against.  Callers pass the
            current time; the default of ``0.0`` is only for the numeric form,
            which does not consult it.

    Returns:
        A non-negative number of seconds, or ``None`` when the header is
        absent, unparseable, or asks for a time already past.

    Example::

        delay = retry_after_seconds(resp, ceiling=30.0, now=time.time())
    """
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    text = raw.strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            target = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        seconds = target.timestamp() - now
    if seconds != seconds or seconds <= 0:  # NaN, or a moment already past
        return None
    return min(seconds, ceiling)
