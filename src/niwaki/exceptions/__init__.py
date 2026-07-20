"""
Niwaki SDK — public exception hierarchy.

All errors raised by the SDK are subclasses of ``NiwakiError`` so callers can
write a single broad ``except NiwakiError`` or target a specific branch:

.. code-block:: python

    from niwaki.exceptions import (
        NiwakiError,          # catch-all
        AuthError,            # any authentication failure
        LoginError,           # wrong credentials
        TokenRefreshError,    # /aaaRefresh.json failed
        SessionExpiredError,  # token dead, re-login also failed
        TransportError,       # any network-level error
        ConnectionError,      # host unreachable
        TimeoutError,         # request too slow
        TLSError,             # SSL/TLS certificate issue
        APIError,             # APIC returned 4xx / 5xx
        UnauthorizedError,    # 401 — token rejected by APIC
        ForbiddenError,       # 403 — insufficient privileges
        NotFoundError,        # 404 — MO does not exist
        ServerError,          # 5xx — APIC internal error
        DeserializationError, # response cannot be parsed into a typed model
        StagedPushError,      # staged design push partially succeeded
        SubscriptionError,             # any object-subscription (WebSocket push) failure
        StatsClassNotSubscribableError,  # subscribed to a stats class — never pushes
        SubscribeRejectedError,        # the APIC rejected a subscription=yes request
        SubscriptionLostError,         # a subscription could not be recovered
        SubscriptionLostReason,        # which recovery path was exhausted (see .reason)
    )

Hierarchy::

    NiwakiError
    ├── AuthError
    │   ├── LoginError
    │   ├── TokenRefreshError
    │   └── SessionExpiredError
    ├── TransportError
    │   ├── ConnectionError
    │   ├── TimeoutError
    │   └── TLSError
    ├── APIError
    │   ├── UnauthorizedError
    │   ├── ForbiddenError
    │   ├── NotFoundError
    │   └── ServerError
    ├── DeserializationError
    ├── NoResultError               (query .one() matched nothing)
    ├── MultipleResultsError        (query .one() matched several)
    ├── DesignError
    │   ├── UnknownMakerError          (also an AttributeError)
    │   ├── DuplicateDeclarationError
    │   ├── UnresolvedReferenceError
    │   ├── AmbiguousBindError
    │   └── StagedPushError
    └── SubscriptionError
        ├── StatsClassNotSubscribableError
        ├── SubscribeRejectedError      (also an APIError)
        └── SubscriptionLostError       (.reason: SubscriptionLostReason)
"""

from __future__ import annotations

from niwaki.exceptions._api import (
    APIError,
    ForbiddenError,
    NotFoundError,
    ServerError,
    UnauthorizedError,
)
from niwaki.exceptions._auth import (
    AuthError,
    LoginError,
    SessionExpiredError,
    TokenRefreshError,
)
from niwaki.exceptions._base import NiwakiError
from niwaki.exceptions._design import (
    AmbiguousBindError,
    DesignError,
    DuplicateDeclarationError,
    StagedPushError,
    UnknownMakerError,
    UnresolvedReferenceError,
)
from niwaki.exceptions._models import DeserializationError
from niwaki.exceptions._query import MultipleResultsError, NoResultError
from niwaki.exceptions._subscription import (
    StatsClassNotSubscribableError,
    SubscribeRejectedError,
    SubscriptionError,
    SubscriptionLostError,
    SubscriptionLostReason,
)
from niwaki.exceptions._transport import (
    ConnectionError,
    TimeoutError,
    TLSError,
    TransportError,
)

__all__ = [
    "APIError",
    "AmbiguousBindError",
    "AuthError",
    "ConnectionError",
    "DeserializationError",
    "DesignError",
    "DuplicateDeclarationError",
    "ForbiddenError",
    "LoginError",
    "MultipleResultsError",
    "NiwakiError",
    "NoResultError",
    "NotFoundError",
    "ServerError",
    "SessionExpiredError",
    "StagedPushError",
    "StatsClassNotSubscribableError",
    "SubscribeRejectedError",
    "SubscriptionError",
    "SubscriptionLostError",
    "SubscriptionLostReason",
    "TLSError",
    "TimeoutError",
    "TokenRefreshError",
    "TransportError",
    "UnauthorizedError",
    "UnknownMakerError",
    "UnresolvedReferenceError",
]
