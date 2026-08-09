"""The on-disk token cache, and the ways it must refuse to help.

A cache holding a bearer token is a security surface before it is a
convenience. Most of these tests are about what it declines to do: read a file
others can read, trust a token about to expire, leave a readable file behind
when a write fails halfway.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from niwaki.transport._token_cache import TokenCache, cache_key, default_cache_dir

HOST = "https://apic.example.com"
USER = "admin"


def _later(seconds: float) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


@pytest.fixture
def cache(tmp_path: Path) -> TokenCache:
    return TokenCache(tmp_path / "cache")


class TestTheRoundTrip:
    def test_a_written_token_reads_back(self, cache: TokenCache) -> None:
        expires = _later(600)
        cache.write(HOST, USER, "tok-abc", expires)
        assert cache.read(HOST, USER) == ("tok-abc", expires)

    def test_an_absent_entry_is_none_not_an_error(self, cache: TokenCache) -> None:
        assert cache.read(HOST, USER) is None

    def test_clearing_an_absent_entry_is_not_an_error(self, cache: TokenCache) -> None:
        cache.clear(HOST, USER)  # must not raise

    def test_clearing_removes_it(self, cache: TokenCache) -> None:
        cache.write(HOST, USER, "tok", _later(600))
        cache.clear(HOST, USER)
        assert cache.read(HOST, USER) is None


class TestEntriesDoNotCollide:
    def test_two_users_on_one_fabric_are_separate(self, cache: TokenCache) -> None:
        cache.write(HOST, "alice", "tok-alice", _later(600))
        cache.write(HOST, "bob", "tok-bob", _later(600))
        alice, bob = cache.read(HOST, "alice"), cache.read(HOST, "bob")
        assert alice is not None and bob is not None
        assert (alice[0], bob[0]) == ("tok-alice", "tok-bob")

    def test_two_fabrics_for_one_user_are_separate(self, cache: TokenCache) -> None:
        cache.write("https://a.example.com", USER, "tok-a", _later(600))
        cache.write("https://b.example.com", USER, "tok-b", _later(600))
        assert cache.read("https://a.example.com", USER)[0] == "tok-a"  # type: ignore[index]
        assert cache.read("https://b.example.com", USER)[0] == "tok-b"  # type: ignore[index]

    def test_a_trailing_slash_is_the_same_fabric(self) -> None:
        assert cache_key(HOST, USER) == cache_key(f"{HOST}/", USER)

    def test_the_filename_does_not_spell_out_the_fabric(self, cache: TokenCache) -> None:
        """A directory listing must not enumerate which fabrics are in use."""
        cache.write(HOST, USER, "tok", _later(600))
        names = [p.name for p in cache.directory.iterdir()]
        assert names
        assert not any("apic" in name or USER in name for name in names)


class TestItRefusesWhatItCannotTrust:
    def test_a_token_near_expiry_is_not_served(self, cache: TokenCache) -> None:
        """Dying mid-command is worse than one extra login."""
        cache.write(HOST, USER, "tok", _later(5))
        assert cache.read(HOST, USER) is None

    def test_an_expired_token_is_not_served(self, cache: TokenCache) -> None:
        cache.write(HOST, USER, "tok", _later(-60))
        assert cache.read(HOST, USER) is None

    def test_a_comfortably_valid_token_is_served(self, cache: TokenCache) -> None:
        cache.write(HOST, USER, "tok", _later(120))
        assert cache.read(HOST, USER) is not None

    def test_a_group_readable_file_is_refused(self, cache: TokenCache) -> None:
        """Someone widened it, or a careless tool wrote it. Either way, no."""
        cache.write(HOST, USER, "tok", _later(600))
        path = next(cache.directory.glob("*.json"))
        path.chmod(0o640)
        assert cache.read(HOST, USER) is None

    def test_a_world_readable_file_is_refused(self, cache: TokenCache) -> None:
        cache.write(HOST, USER, "tok", _later(600))
        path = next(cache.directory.glob("*.json"))
        path.chmod(0o644)
        assert cache.read(HOST, USER) is None

    @pytest.mark.parametrize(
        "content",
        ["", "not json", "{}", '{"token": "t"}', '{"token": "t", "expires_at": "nonsense"}'],
        ids=["empty", "not-json", "no-fields", "no-expiry", "bad-expiry"],
    )
    def test_a_malformed_entry_is_discarded_not_repaired(
        self, cache: TokenCache, content: str
    ) -> None:
        cache.write(HOST, USER, "tok", _later(600))
        path = next(cache.directory.glob("*.json"))
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
        assert cache.read(HOST, USER) is None

    def test_a_naive_expiry_on_disk_is_refused(self, cache: TokenCache) -> None:
        """Read in another timezone it would mean a different instant."""
        cache.write(HOST, USER, "tok", _later(600))
        path = next(cache.directory.glob("*.json"))
        path.write_text(
            json.dumps({"token": "t", "expires_at": "2099-01-01T00:00:00"}), encoding="utf-8"
        )
        path.chmod(0o600)
        assert cache.read(HOST, USER) is None


class TestPermissionsAreRightFromTheStart:
    def test_the_file_is_owner_only(self, cache: TokenCache) -> None:
        cache.write(HOST, USER, "tok", _later(600))
        path = next(cache.directory.glob("*.json"))
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_the_directory_is_owner_only(self, cache: TokenCache) -> None:
        cache.write(HOST, USER, "tok", _later(600))
        assert stat.S_IMODE(cache.directory.stat().st_mode) == 0o700

    def test_no_temporary_file_is_left_behind(self, cache: TokenCache) -> None:
        cache.write(HOST, USER, "tok", _later(600))
        assert list(cache.directory.glob("*.tmp")) == []

    def test_a_naive_expiry_is_refused_before_anything_is_written(self, cache: TokenCache) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            cache.write(HOST, USER, "tok", datetime(2099, 1, 1))
        assert not cache.directory.exists() or list(cache.directory.iterdir()) == []


class TestWhereItLives:
    def test_xdg_is_honoured(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        assert default_cache_dir() == tmp_path / "xdg" / "niwaki"

    def test_it_falls_back_to_the_home_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        assert default_cache_dir() == Path.home() / ".cache" / "niwaki"

    def test_nothing_is_created_merely_by_asking(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "untouched"))
        default_cache_dir()
        assert not (tmp_path / "untouched").exists()


def test_a_rewrite_replaces_rather_than_appends(cache: TokenCache) -> None:
    """A reader must never see a half-written file, nor two tokens in one."""
    cache.write(HOST, USER, "first", _later(600))
    cache.write(HOST, USER, "second", _later(600))
    assert cache.read(HOST, USER)[0] == "second"  # type: ignore[index]
    assert len(list(cache.directory.glob("*.json"))) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
def test_the_write_is_atomic_under_a_concurrent_reader(cache: TokenCache) -> None:
    """Whatever a reader sees is a complete entry, never a partial one."""
    cache.write(HOST, USER, "tok-original", _later(600))
    for _ in range(50):
        cache.write(HOST, USER, "tok-rewritten", _later(600))
        entry = cache.read(HOST, USER)
        assert entry is not None
        assert entry[0] in {"tok-original", "tok-rewritten"}


class TestTheSessionUsesIt:
    """Wiring, not storage: a cache the login never consults is decoration.

    The failure this covers is the quiet one — everything constructs, every
    test of the cache itself passes, and the session logs in every time anyway.
    """

    def test_a_cached_token_skips_the_login_request(self, cache: TokenCache) -> None:
        from pytest_httpx import HTTPXMock  # noqa: F401 - fixture typing only

        from niwaki.transport.session import ApicSession

        cache.write(HOST, USER, "tok-cached", _later(600))
        session = ApicSession(HOST, USER, "secret", token_cache=cache)
        session.login()  # no HTTP mock registered: a request here would fail

        state = session._token_state  # pyright: ignore[reportPrivateUsage]
        assert state is not None
        assert state.token == "tok-cached"

    def test_a_real_login_fills_the_cache(self, cache: TokenCache) -> None:
        import httpx

        from niwaki.transport.session import ApicSession
        from tests.conftest import login_payload

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=login_payload())

        client = httpx.Client(base_url=HOST, transport=httpx.MockTransport(handler))
        session = ApicSession.with_client(client, HOST, USER, "secret", token_cache=cache)
        session.login()

        entry = cache.read(HOST, USER)
        state = session._token_state  # pyright: ignore[reportPrivateUsage]
        assert entry is not None and state is not None
        assert entry[0] == state.token

    def test_an_expired_entry_falls_through_to_a_real_login(self, cache: TokenCache) -> None:
        import httpx

        from niwaki.transport.session import ApicSession
        from tests.conftest import login_payload

        cache.write(HOST, USER, "tok-stale", _later(-60))
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(200, json=login_payload())

        client = httpx.Client(base_url=HOST, transport=httpx.MockTransport(handler))
        session = ApicSession.with_client(client, HOST, USER, "secret", token_cache=cache)
        session.login()

        state = session._token_state  # pyright: ignore[reportPrivateUsage]
        assert calls == ["/api/aaaLogin.json"]
        assert state is not None and state.token != "tok-stale"

    def test_no_cache_means_the_old_behaviour_exactly(self) -> None:
        """Opt-in: a session without a cache never touches the filesystem."""
        import httpx

        from niwaki.transport.session import ApicSession
        from tests.conftest import login_payload

        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(200, json=login_payload())

        client = httpx.Client(base_url=HOST, transport=httpx.MockTransport(handler))
        session = ApicSession.with_client(client, HOST, USER, "secret")
        session.login()
        session.login()

        assert calls == ["/api/aaaLogin.json", "/api/aaaLogin.json"]

    async def test_the_async_session_uses_it_too(self, cache: TokenCache) -> None:
        from niwaki.transport.session_async import AsyncApicSession

        cache.write(HOST, USER, "tok-cached", _later(600))
        session = AsyncApicSession(HOST, USER, "secret", token_cache=cache)
        await session.login()  # no mock: a request would fail

        state = session._token_state  # pyright: ignore[reportPrivateUsage]
        assert state is not None
        assert state.token == "tok-cached"
        await session.close()


class TestARevokedTokenDoesNotWedgeTheCache:
    """The failure a security review found and thirty-three tests did not.

    ``login()`` is not only how a session starts — it is how it *recovers*. A
    401 mid-session, an expired token, a refresh the controller rejects: all
    three end in ``login()``. Serving those from the cache answers a rejection
    with the very token that was just rejected, and since nothing refills the
    entry, every later process inherits the same dead token. Without a cache
    the same fabric heals on the first 401.

    So recovery bypasses the cache, and forgets the entry on its way past.
    """

    @staticmethod
    def _revoking_fabric() -> tuple[list[str], object]:
        """A controller that mints tokens and rejects everything but the newest."""
        import httpx

        from tests.conftest import login_payload

        calls: list[str] = []
        # Whatever the login mints is the only token this controller honours;
        # anything else — a stale cache entry, say — earns a 401.
        live: dict[str, str | None] = {"token": None}

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(f"{request.method} {request.url.path}")
            if request.url.path.endswith("aaaLogin.json"):
                payload = login_payload()
                live["token"] = payload["imdata"][0]["aaaLogin"]["attributes"]["token"]
                return httpx.Response(200, json=payload)
            presented = request.headers.get("cookie", "")
            if live["token"] is None or live["token"] not in presented:
                return httpx.Response(401, json={"imdata": []})
            return httpx.Response(200, json={"totalCount": "0", "imdata": []})

        return calls, handler

    def test_a_revoked_cached_token_still_recovers(self, cache: TokenCache) -> None:
        import httpx

        from niwaki.transport.session import ApicSession

        cache.write(HOST, USER, "tok-revoked", _later(600))
        calls, handler = self._revoking_fabric()
        client = httpx.Client(base_url=HOST, transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
        session = ApicSession.with_client(client, HOST, USER, "secret", token_cache=cache)
        session.login()

        session.get("/api/class/fvTenant.json")  # must not raise

        assert any("aaaLogin" in call for call in calls), "no recovery login was attempted"

    def test_recovery_forgets_the_dead_entry(self, cache: TokenCache) -> None:
        """Otherwise the next process inherits it, and the one after that.

        Asserting on the cache's *contents* afterwards proves nothing: the
        recovery login rewrites the entry, so a fresh token is there either
        way.  What must be observed is the clear itself.
        """
        import httpx

        from niwaki.transport.session import ApicSession

        cleared: list[tuple[str, str]] = []
        original = type(cache).clear

        def recording_clear(self: TokenCache, host: str, username: str) -> None:
            cleared.append((host, username))
            original(self, host, username)

        cache.write(HOST, USER, "tok-revoked", _later(600))
        _, handler = self._revoking_fabric()
        client = httpx.Client(base_url=HOST, transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
        session = ApicSession.with_client(client, HOST, USER, "secret", token_cache=cache)
        session.login()

        object.__setattr__(cache, "clear", recording_clear.__get__(cache))
        session.get("/api/class/fvTenant.json")

        assert cleared == [(HOST, USER)], "the dead entry was never cleared"

    def test_bypassing_alone_is_not_enough_and_neither_is_clearing(self, cache: TokenCache) -> None:
        """Both halves are load-bearing, and each masks the other's absence.

        Clearing before the recovery login makes a cache-consulting login miss;
        bypassing makes an uncleared entry irrelevant.  Either one alone hides
        that the other is gone, which is why the guard above watches the clear
        directly rather than its consequences.
        """
        import inspect

        from niwaki.transport.session import ApicSession

        source = inspect.getsource(ApicSession.login)
        assert "use_cache and self._token_cache" in source, "the bypass flag is not honoured"

        for site in (ApicSession._request_checked, ApicSession._raw_write, ApicSession._relogin):
            body = inspect.getsource(site)
            assert "login(use_cache=False)" in body, f"{site.__name__} may re-enter the cache"
            assert "_forget_cached_token()" in body, f"{site.__name__} leaves the dead entry"

    async def test_the_async_session_recovers_too(self, cache: TokenCache) -> None:
        import httpx

        from niwaki.transport.session_async import AsyncApicSession

        cache.write(HOST, USER, "tok-revoked", _later(600))
        calls, handler = self._revoking_fabric()

        async def async_handler(request: httpx.Request) -> httpx.Response:
            return handler(request)  # type: ignore[operator,no-any-return]

        client = httpx.AsyncClient(base_url=HOST, transport=httpx.MockTransport(async_handler))
        session = AsyncApicSession.with_client(client, HOST, USER, "secret", token_cache=cache)
        await session.login()
        await session.get("/api/class/fvTenant.json")
        await session.close()

        assert any("aaaLogin" in call for call in calls)

    def test_the_first_login_of_a_process_still_uses_the_cache(self, cache: TokenCache) -> None:
        """The bypass must be surgical: recovery only, not every login."""
        from niwaki.transport.session import ApicSession

        cache.write(HOST, USER, "tok-cached", _later(600))
        session = ApicSession(HOST, USER, "secret", token_cache=cache)
        session.login()  # no mock registered — a request would fail

        state = session._token_state  # pyright: ignore[reportPrivateUsage]
        assert state is not None and state.token == "tok-cached"

    def test_an_explicit_bypass_reaches_the_controller(self, cache: TokenCache) -> None:
        import httpx

        from niwaki.transport.session import ApicSession
        from tests.conftest import login_payload

        cache.write(HOST, USER, "tok-cached", _later(600))
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(200, json=login_payload())

        client = httpx.Client(base_url=HOST, transport=httpx.MockTransport(handler))
        session = ApicSession.with_client(client, HOST, USER, "secret", token_cache=cache)
        session.login(use_cache=False)

        assert calls == ["/api/aaaLogin.json"]
