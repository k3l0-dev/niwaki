"""
Unit tests for ``niwaki.transport._token.TokenState``.

Covers:
- ``is_expired`` and ``needs_refresh`` states relative to the current time.
- ``from_apic_response`` factory.
- Edge cases: token expired at the exact instant, custom threshold.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from niwaki.transport._token import TokenState

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_token(
    ttl_seconds: int,
    threshold_seconds: int = 60,
) -> TokenState:
    """Build a TokenState that expires in ``ttl_seconds`` seconds."""
    return TokenState.from_apic_response(
        token="test-token",
        refresh_timeout_seconds=ttl_seconds,
        refresh_threshold=timedelta(seconds=threshold_seconds),
    )


# ── is_expired ────────────────────────────────────────────────────────────────


class TestIsExpired:
    def test_fresh_token_not_expired(self) -> None:
        """A freshly created token with TTL > 0 is not expired."""
        state = _make_token(ttl_seconds=600)
        assert not state.is_expired()

    def test_expired_token(self) -> None:
        """A token whose expires_at is in the past is expired."""
        state = TokenState(
            token="old",
            expires_at=datetime.now(tz=UTC) - timedelta(seconds=1),
        )
        assert state.is_expired()

    def test_token_expiring_exactly_now(self) -> None:
        """A token whose expires_at is the current instant is considered expired."""
        state = TokenState(
            token="now",
            expires_at=datetime.now(tz=UTC),
        )
        # Comparison is >=, so expired when equal to now.
        assert state.is_expired()

    def test_token_expiring_in_future_not_expired(self) -> None:
        """A token expiring in 1 second is not yet expired."""
        state = TokenState(
            token="soon",
            expires_at=datetime.now(tz=UTC) + timedelta(seconds=1),
        )
        assert not state.is_expired()


# ── needs_refresh ─────────────────────────────────────────────────────────────


class TestNeedsRefresh:
    def test_far_from_expiry_no_refresh_needed(self) -> None:
        """A token expiring in 10 min with a 60 s threshold does not need refresh."""
        state = _make_token(ttl_seconds=600, threshold_seconds=60)
        assert not state.needs_refresh()

    def test_within_threshold_needs_refresh(self) -> None:
        """A token expiring in 30 s with a 60 s threshold needs refresh."""
        state = TokenState(
            token="x",
            expires_at=datetime.now(tz=UTC) + timedelta(seconds=30),
            refresh_threshold=timedelta(seconds=60),
        )
        assert state.needs_refresh()

    def test_expired_token_also_needs_refresh(self) -> None:
        """An expired token returns True for both needs_refresh and is_expired."""
        state = TokenState(
            token="x",
            expires_at=datetime.now(tz=UTC) - timedelta(seconds=10),
            refresh_threshold=timedelta(seconds=60),
        )
        assert state.is_expired()
        assert state.needs_refresh()

    def test_custom_threshold_respected(self) -> None:
        """A 120 s threshold triggers refresh when 90 s remain."""
        state = TokenState(
            token="x",
            expires_at=datetime.now(tz=UTC) + timedelta(seconds=90),
            refresh_threshold=timedelta(seconds=120),
        )
        assert state.needs_refresh()

    def test_token_exactly_at_threshold_boundary(self) -> None:
        """A token with exactly ``threshold`` seconds remaining must be refreshed."""
        threshold = timedelta(seconds=60)
        state = TokenState(
            token="x",
            expires_at=datetime.now(tz=UTC) + threshold,
            refresh_threshold=threshold,
        )
        # expires_at - threshold == now → needs_refresh() == True
        assert state.needs_refresh()


# ── from_apic_response ────────────────────────────────────────────────────────


class TestFromApicResponse:
    def test_basic_construction(self) -> None:
        """The factory creates a TokenState with the correct token and a future expiry."""
        state = TokenState.from_apic_response(
            token="abc123",
            refresh_timeout_seconds=600,
        )
        assert state.token == "abc123"
        assert state.expires_at > datetime.now(tz=UTC)
        assert not state.is_expired()

    def test_ttl_is_approximately_correct(self) -> None:
        """The expiry is computed at approximately TTL seconds in the future."""
        ttl = 600
        before = datetime.now(tz=UTC)
        state = TokenState.from_apic_response(token="t", refresh_timeout_seconds=ttl)
        after = datetime.now(tz=UTC)

        expected_min = before + timedelta(seconds=ttl)
        expected_max = after + timedelta(seconds=ttl)
        assert expected_min <= state.expires_at <= expected_max

    def test_default_threshold_is_60_seconds(self) -> None:
        """Without a ``refresh_threshold`` argument, the default threshold is 60 seconds."""
        state = TokenState.from_apic_response(token="t", refresh_timeout_seconds=600)
        assert state.refresh_threshold == timedelta(seconds=60)

    def test_custom_threshold(self) -> None:
        """A custom threshold is stored correctly."""
        custom = timedelta(seconds=120)
        state = TokenState.from_apic_response(
            token="t",
            refresh_timeout_seconds=600,
            refresh_threshold=custom,
        )
        assert state.refresh_threshold == custom

    def test_zero_ttl_is_immediately_expired(self) -> None:
        """A TTL of 0 seconds produces an immediately expired token."""
        state = TokenState.from_apic_response(token="t", refresh_timeout_seconds=0)
        assert state.is_expired()

    @pytest.mark.parametrize("ttl", [1, 60, 300, 600, 3600, 86400])
    def test_various_ttl_values(self, ttl: int) -> None:
        """The factory works for various TTL values without raising."""
        state = TokenState.from_apic_response(token="t", refresh_timeout_seconds=ttl)
        assert state.token == "t"
        # All TTL >= 1 must produce a non-expired token at creation time.
        assert not state.is_expired()
