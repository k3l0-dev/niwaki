"""Design push engine — async waves against AsyncMoWriter stubs.

Mirror of the sync engine tests plus the async-only guarantees: intra-wave
concurrency uses ``asyncio.gather``, and any :class:`AsyncMoWriter`-conforming
stub is a valid session (no real HTTP anywhere).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from niwaki.design._engine import _Op, _run_waves
from niwaki.transport._protocols import AsyncMoWriter


def _make_session(post_side_effect: Any = None) -> MagicMock:
    session = MagicMock()
    session.post_mo = AsyncMock(side_effect=post_side_effect)
    session.delete_mo = AsyncMock()
    return session


def _op(dn: str, method: str = "POST") -> _Op:
    return _Op(dn=dn, method=method, payload={} if method == "POST" else None)  # type: ignore[arg-type]


class TestRunWaves:
    async def test_all_succeed_in_wave_order(self) -> None:
        call_order: list[str] = []

        async def _post(dn: str, payload: Any) -> None:
            call_order.append(dn)

        session = _make_session(post_side_effect=_post)
        outcome = await _run_waves(session, [_op("uni/tn-p/BD-w"), _op("uni/tn-p")])
        assert outcome.ok
        assert call_order == ["uni/tn-p", "uni/tn-p/BD-w"]

    async def test_delete_op_uses_delete_mo(self) -> None:
        session = _make_session()
        outcome = await _run_waves(session, [_op("uni/tn-p", method="DELETE")])
        assert outcome.ok
        session.delete_mo.assert_called_once_with("uni/tn-p")

    async def test_failure_stops_next_waves(self) -> None:
        session = _make_session(post_side_effect=RuntimeError("boom"))
        ops = [_op("uni/tn-p"), _op("uni/tn-p/BD-w")]
        outcome = await _run_waves(session, ops)
        assert not outcome.ok
        assert [op.dn for op, _ in outcome.failed] == ["uni/tn-p"]
        assert [op.dn for op in outcome.not_run] == ["uni/tn-p/BD-w"]

    async def test_failure_within_wave_attempts_siblings(self) -> None:
        async def _post(dn: str, payload: Any) -> None:
            if "bad" in dn:
                raise RuntimeError("boom")

        session = _make_session(post_side_effect=_post)
        ops = [_op("uni/tn-p"), _op("uni/tn-p/BD-good"), _op("uni/tn-p/BD-bad")]
        outcome = await _run_waves(session, ops)
        assert [op.dn for op in outcome.succeeded] == ["uni/tn-p", "uni/tn-p/BD-good"]
        assert [op.dn for op, _ in outcome.failed] == ["uni/tn-p/BD-bad"]

    async def test_continue_on_failure_runs_all_waves(self) -> None:
        async def _post(dn: str, payload: Any) -> None:
            if dn == "uni/tn-p":
                raise RuntimeError("boom")

        session = _make_session(post_side_effect=_post)
        ops = [_op("uni/tn-p"), _op("uni/tn-p/BD-w")]
        outcome = await _run_waves(session, ops, continue_on_failure=True)
        assert [op.dn for op in outcome.succeeded] == ["uni/tn-p/BD-w"]
        assert outcome.not_run == []

    async def test_empty_ops(self) -> None:
        session = _make_session()
        outcome = await _run_waves(session, [])
        assert outcome.ok
        session.post_mo.assert_not_called()


class TestAsyncMoWriterProtocol:
    def test_async_apic_session_satisfies_protocol(self) -> None:
        from niwaki.transport.session_async import AsyncApicSession

        assert issubclass(AsyncApicSession, AsyncMoWriter)

    def test_minimal_stub_satisfies_protocol(self) -> None:
        class _Stub:
            async def post_mo(self, dn: str, payload: dict[str, Any]) -> None: ...
            async def delete_mo(self, dn: str) -> None: ...

        assert isinstance(_Stub(), AsyncMoWriter)

    def test_missing_delete_mo_fails_protocol(self) -> None:
        class _Incomplete:
            async def post_mo(self, dn: str, payload: dict[str, Any]) -> None: ...

        assert not isinstance(_Incomplete(), AsyncMoWriter)

    def test_sync_writer_protocol_covers_sync_session(self) -> None:
        from niwaki.transport._protocols import MoWriter
        from niwaki.transport.session import ApicSession

        assert issubclass(ApicSession, MoWriter)
