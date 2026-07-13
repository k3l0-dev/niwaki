"""Transport layer — APIC sessions, retry policy, and the transport boundary.

The clients (:class:`~niwaki.Niwaki`, :class:`~niwaki.AsyncNiwaki`) own a
session and hand it to the design engine and the query builder through two
pairs of **structural protocols** — :class:`MoWriter` / :class:`AsyncMoWriter`
(write) and :class:`MoReader` / :class:`AsyncMoReader` (read).  Anything that
implements them is a valid transport, which is what makes the SDK testable
without a fabric (see the *Testing your automation* guide).

Sessions are managed by the clients; construct one directly only when you
need a transport without the facade.
"""

from niwaki.transport._config import RetryConfig
from niwaki.transport._protocols import (
    AsyncMoReader,
    AsyncMoWriter,
    MoReader,
    MoWriter,
)
from niwaki.transport.session import ApicSession
from niwaki.transport.session_async import AsyncApicSession

__all__ = [
    "ApicSession",
    "AsyncApicSession",
    "AsyncMoReader",
    "AsyncMoWriter",
    "MoReader",
    "MoWriter",
    "RetryConfig",
]
