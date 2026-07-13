"""Transport-layer configuration value objects.

:class:`RetryConfig` controls the stamina retry behaviour for all APIC HTTP
requests.  Pass an instance to :class:`~niwaki.transport.session.ApicSession`
or :class:`~niwaki.transport.session_async.AsyncApicSession` (or to
:class:`~niwaki.Niwaki` / :class:`~niwaki.AsyncNiwaki`) to
override the defaults.

Example::

    from niwaki.transport import RetryConfig

    # No retries in unit tests — prevents spurious delays
    with ApicSession(host, user, pwd, retry=RetryConfig(attempts=1)) as s:
        ...

    # More aggressive retry for an unreliable WAN link
    async with AsyncNiwaki(host, user, pwd, retry=RetryConfig(attempts=5, wait_max=30.0)) as aci:
        ...
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryConfig:
    """Stamina retry parameters for APIC HTTP transport.

    A frozen (immutable, hashable) value object.  All retry parameters are
    forwarded verbatim to ``stamina.retry_context`` / ``stamina.retry``
    for both GET and mutating requests.

    Args:
        attempts: Total number of attempts (first try + retries).  ``1``
            effectively disables retries.  Default: ``3``.
        wait_initial: Initial backoff in seconds before the first retry.
            Default: ``0.5``.
        wait_max: Maximum backoff in seconds (exponential backoff is capped
            here).  Default: ``5.0``.
        wait_jitter: Random jitter added to each backoff in seconds to
            prevent thundering-herd effects.  Default: ``0.5``.

    Example::

        # Disable retries entirely (one attempt, no backoff)
        no_retry = RetryConfig(attempts=1)

        # Production: three attempts with up to 10 s max backoff
        prod = RetryConfig(attempts=3, wait_max=10.0)
    """

    attempts: int = 3
    wait_initial: float = 0.5
    wait_max: float = 5.0
    wait_jitter: float = 0.5
