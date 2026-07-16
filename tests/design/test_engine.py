"""Design push engine — _Op, bracket-aware depth, toposort, sync waves.

The engine is internal: these tests pin its behaviour so
the public ``push()`` semantics (ordering, failure accounting) cannot drift.
No I/O — the sync engine takes a plain callable.
"""

from __future__ import annotations

import pytest

from niwaki.design._engine import _Op, _run_waves_sync, _toposort


def _op(dn: str, method: str = "POST") -> _Op:
    return _Op(dn=dn, method=method, payload={} if method == "POST" else None)  # type: ignore[arg-type]


class TestOpDepth:
    def test_root_depth_zero(self) -> None:
        assert _op("uni").depth == 0

    def test_one_segment_depth_one(self) -> None:
        assert _op("uni/tn-prod").depth == 1

    def test_two_segments_depth_two(self) -> None:
        assert _op("uni/tn-prod/BD-web").depth == 2

    def test_bracketed_slash_is_not_a_segment(self) -> None:
        assert _op("uni/tn-p/BD-w/subnet-[10.0.1.1/24]").depth == 3

    def test_nested_brackets(self) -> None:
        dn = "uni/tn-p/ap-a/epg-e/rspathAtt-[topology/pod-1/paths-101/pathep-[eth1/1]]"
        assert _op(dn).depth == 4


class TestOpImmutable:
    def test_frozen_raises_on_mutation(self) -> None:
        op = _op("uni/tn-prod")
        with pytest.raises(AttributeError):
            op.dn = "uni/tn-other"  # type: ignore[misc]

    def test_hashable_and_comparable(self) -> None:
        op1 = _Op(dn="uni/tn-p", method="POST", payload=None)
        op2 = _Op(dn="uni/tn-p", method="POST", payload=None)
        assert op1 == op2
        assert len({op1, op2}) == 1
        assert op1 != _Op(dn="uni/tn-p", method="DELETE", payload=None)


class TestToposort:
    def test_empty_input(self) -> None:
        assert _toposort([]) == []

    def test_linear_dependency(self) -> None:
        bd, tn = _op("uni/tn-p/BD-w"), _op("uni/tn-p")
        assert _toposort([bd, tn]) == [[tn], [bd]]

    def test_parallel_siblings_in_same_wave(self) -> None:
        tn = _op("uni/tn-p")
        bd, ctx = _op("uni/tn-p/BD-w"), _op("uni/tn-p/ctx-v")
        waves = _toposort([bd, tn, ctx])
        assert waves[0] == [tn]
        assert waves[1] == [bd, ctx]  # order within a wave is stable

    def test_depth_gaps_are_fine(self) -> None:
        deep = _op("uni/tn-p/BD-w/subnet-[10.0.0.1/24]")
        shallow = _op("uni/tn-p")
        assert _toposort([deep, shallow]) == [[shallow], [deep]]


class TestRunWavesSync:
    def test_all_succeed(self) -> None:
        executed: list[str] = []
        ops = [_op("uni/tn-p/BD-w"), _op("uni/tn-p")]
        outcome = _run_waves_sync(lambda op: executed.append(op.dn), ops)
        assert outcome.ok
        assert executed == ["uni/tn-p", "uni/tn-p/BD-w"]
        assert [op.dn for op in outcome.succeeded] == executed

    def test_descendant_of_failure_is_skipped(self) -> None:
        err = RuntimeError("boom")

        def _execute(op: _Op) -> None:
            if op.dn == "uni/tn-p":
                raise err

        ops = [_op("uni/tn-p"), _op("uni/tn-p/BD-w")]
        outcome = _run_waves_sync(_execute, ops)
        assert not outcome.ok
        assert outcome.failed == [(ops[0], err)]
        # BD-w descends from the failed tenant → skipped, not attempted.
        assert [op.dn for op in outcome.not_run] == ["uni/tn-p/BD-w"]

    def test_failure_within_wave_attempts_siblings(self) -> None:
        def _execute(op: _Op) -> None:
            if "bad" in op.dn:
                raise RuntimeError("boom")

        ops = [_op("uni/tn-p/BD-bad"), _op("uni/tn-p/BD-good")]
        outcome = _run_waves_sync(_execute, ops)
        assert [op.dn for op in outcome.succeeded] == ["uni/tn-p/BD-good"]
        assert [op.dn for op, _ in outcome.failed] == ["uni/tn-p/BD-bad"]
        assert outcome.not_run == []

    def test_independent_branch_survives_failure(self) -> None:
        """A failure isolates its own subtree; a sibling branch still lands."""

        def _execute(op: _Op) -> None:
            if op.dn == "uni/tn-p/BD-a":
                raise RuntimeError("boom")

        ops = [
            _op("uni/tn-p"),
            _op("uni/tn-p/BD-a"),
            _op("uni/tn-p/BD-b"),
            _op("uni/tn-p/BD-a/subnet-[10.0.0.1/24]"),
            _op("uni/tn-p/BD-b/subnet-[10.0.1.1/24]"),
        ]
        outcome = _run_waves_sync(_execute, ops)
        assert not outcome.ok
        assert [op.dn for op, _ in outcome.failed] == ["uni/tn-p/BD-a"]
        # BD-b and its subnet land — only BD-a's own subnet is skipped.
        assert "uni/tn-p/BD-b/subnet-[10.0.1.1/24]" in [op.dn for op in outcome.succeeded]
        assert [op.dn for op in outcome.not_run] == ["uni/tn-p/BD-a/subnet-[10.0.0.1/24]"]

    def test_delete_ops_flow_through(self) -> None:
        methods: list[str] = []
        outcome = _run_waves_sync(
            lambda op: methods.append(op.method), [_op("uni/tn-p", method="DELETE")]
        )
        assert outcome.ok
        assert methods == ["DELETE"]

    def test_empty_ops(self) -> None:
        outcome = _run_waves_sync(lambda op: None, [])
        assert outcome.ok
        assert outcome.succeeded == []
