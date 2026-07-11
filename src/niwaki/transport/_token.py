"""
Internal APIC session token state.

This module is private (``_`` prefix): only import from ``niwaki.transport.session``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta


@dataclass
class TokenState:
    """
    Encapsulates an APIC session token and its expiry metadata.

    The token is returned by ``/api/aaaLogin.json`` and renewed by
    ``/api/aaaRefresh.json``. The APIC provides a TTL (``refreshTimeoutSeconds``)
    from which the absolute expiry instant is computed.

    Attributes:
        token: APIC token string, set as the ``APIC-cookie`` cookie.
        expires_at: UTC datetime at which the token becomes invalid.
        refresh_threshold: Duration before ``expires_at`` at which a proactive
            refresh is triggered. Default: 60 seconds.

    Example::

        state = TokenState.from_apic_response(
            token="abc123",
            refresh_timeout_seconds=600,
        )
        if state.needs_refresh():
            session._refresh_token()
    """

    token: str
    expires_at: datetime
    refresh_threshold: timedelta = field(default_factory=lambda: timedelta(seconds=60))

    def is_expired(self) -> bool:
        """
        Return ``True`` if the token has passed its expiry instant.

        An expired token can no longer be refreshed; a full re-login is required.

        Returns:
            ``True`` if ``datetime.now(UTC) >= expires_at``.
        """
        return datetime.now(tz=UTC) >= self.expires_at

    def needs_refresh(self) -> bool:
        """
        Return ``True`` if the token will expire within the configured threshold.

        Used for proactive refresh: trigger ``/api/aaaRefresh.json`` before the
        token actually expires, avoiding mid-request 401 errors.

        Returns:
            ``True`` if ``datetime.now(UTC) >= expires_at - refresh_threshold``.

        Note:
            A token that is already expired (``is_expired() == True``) also
            returns ``True`` here. Check ``is_expired()`` first to choose the
            correct code path.
        """
        return datetime.now(tz=UTC) >= (self.expires_at - self.refresh_threshold)

    @classmethod
    def from_apic_response(
        cls,
        token: str,
        refresh_timeout_seconds: int,
        *,
        refresh_threshold: timedelta | None = None,
    ) -> TokenState:
        """
        Build a ``TokenState`` from data returned by the APIC.

        The absolute expiry is computed as ``now + TTL``.
        Called after login (``aaaLogin``) and after refresh (``aaaRefresh``).

        Args:
            token: Session token returned by the APIC.
            refresh_timeout_seconds: TTL in seconds as returned by the
                ``refreshTimeoutSeconds`` field of the APIC response.
            refresh_threshold: Proactive refresh threshold.
                Defaults to 60 seconds.

        Returns:
            A new ``TokenState`` with ``expires_at`` computed from the current instant.

        Example::

            state = TokenState.from_apic_response(
                token="abc123",
                refresh_timeout_seconds=600,        # 10 min TTL
                refresh_threshold=timedelta(seconds=90),
            )
        """
        expires_at = datetime.now(tz=UTC) + timedelta(seconds=refresh_timeout_seconds)
        threshold = refresh_threshold if refresh_threshold is not None else timedelta(seconds=60)
        return cls(token=token, expires_at=expires_at, refresh_threshold=threshold)
