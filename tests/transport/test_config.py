"""Tests for niwaki.transport._config — RetryConfig value object.

Covers: defaults, custom values, immutability, hashability, and integration
with ApicSession / AsyncApicSession (retry params forwarded correctly).
"""

from __future__ import annotations

import pytest

from niwaki import RetryConfig
from niwaki.transport._config import RetryConfig as RetryConfigDirect

# ── RetryConfig construction ──────────────────────────────────────────────────


class TestRetryConfigDefaults:
    def test_default_attempts(self) -> None:
        assert RetryConfig().attempts == 3

    def test_default_wait_initial(self) -> None:
        assert RetryConfig().wait_initial == 0.5

    def test_default_wait_max(self) -> None:
        assert RetryConfig().wait_max == 5.0

    def test_default_wait_jitter(self) -> None:
        assert RetryConfig().wait_jitter == 0.5

    def test_custom_values_accepted(self) -> None:
        rc = RetryConfig(attempts=5, wait_initial=1.0, wait_max=30.0, wait_jitter=0.0)
        assert rc.attempts == 5
        assert rc.wait_initial == 1.0
        assert rc.wait_max == 30.0
        assert rc.wait_jitter == 0.0

    def test_single_attempt_disables_retry(self) -> None:
        rc = RetryConfig(attempts=1)
        assert rc.attempts == 1


class TestRetryConfigImmutability:
    def test_frozen_raises_on_mutation(self) -> None:
        rc = RetryConfig()
        with pytest.raises((AttributeError, TypeError)):
            rc.attempts = 99  # type: ignore[misc]

    def test_hashable(self) -> None:
        rc1 = RetryConfig(attempts=2)
        rc2 = RetryConfig(attempts=2)
        assert hash(rc1) == hash(rc2)
        assert {rc1, rc2} == {rc1}  # deduplicates in sets

    def test_equality(self) -> None:
        assert RetryConfig() == RetryConfig()
        assert RetryConfig(attempts=1) != RetryConfig(attempts=3)


# ── Export paths ──────────────────────────────────────────────────────────────


class TestRetryConfigExports:
    def test_importable_from_niwaki(self) -> None:
        assert RetryConfig is RetryConfigDirect

    def test_importable_from_transport(self) -> None:
        from niwaki.transport import RetryConfig as TransportRC

        assert TransportRC is RetryConfigDirect


# ── Integration: sessions accept retry ───────────────────────────────────────


class TestRetryConfigIntegration:
    def test_sync_session_stores_retry(self) -> None:
        from niwaki.transport.session import ApicSession

        rc = RetryConfig(attempts=1)
        s = ApicSession("https://apic.test", "u", "p", retry=rc)
        assert s.retry is rc

    def test_async_session_stores_retry(self) -> None:
        from niwaki.transport.session_async import AsyncApicSession

        rc = RetryConfig(attempts=5)
        s = AsyncApicSession("https://apic.test", "u", "p", retry=rc)
        assert s.retry is rc

    def test_sync_session_default_retry_is_retryconfig(self) -> None:
        from niwaki.transport.session import ApicSession

        s = ApicSession("https://apic.test", "u", "p")
        assert isinstance(s.retry, RetryConfig)
        assert s.retry == RetryConfig()

    def test_async_session_default_retry_is_retryconfig(self) -> None:
        from niwaki.transport.session_async import AsyncApicSession

        s = AsyncApicSession("https://apic.test", "u", "p")
        assert isinstance(s.retry, RetryConfig)
        assert s.retry == RetryConfig()
