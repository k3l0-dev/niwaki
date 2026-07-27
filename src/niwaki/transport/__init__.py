"""Transport layer — APIC sessions, retry policy, and the transport boundary.

The clients (:class:`~niwaki.Niwaki`, :class:`~niwaki.AsyncNiwaki`) own a
session and hand it to the design engine, whose wave runner consumes the
:class:`AsyncMoWriter` structural protocol; :class:`MoWriter` and
:class:`MoReader` / :class:`AsyncMoReader` document the same session surface
as checkable shape contracts. Tests fake the HTTP layer underneath the
concrete sessions (see the *Testing your automation* guide).

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
