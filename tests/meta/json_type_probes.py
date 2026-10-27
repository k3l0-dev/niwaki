"""Static probes: realistic user code against the SDK's raw-JSON signatures.

Never imported or executed — not a test module (no ``test_`` prefix).  It is
*type-checked*, twice: by pyright (which covers ``tests/`` in
``scripts/checks.sh --fast``) and by mypy (driven from
``tests/test_json_types.py::test_probes_type_check_under_mypy``).  Every line
must be accepted by both checkers; a rejection is the failure.

The contract it pins:

* **Consumers accept what users actually hold.**  A function that *reads* an
  APIC envelope takes ``Mapping[str, object]`` (``Iterable[...]`` of it for
  ``parse_envelopes``): ``dict`` is invariant, so a ``JsonObject`` parameter
  would reject an unannotated nested literal, a ``dict[str, dict[str, str]]``,
  ``{"imdata": session.get(...)}`` and a payload composed of ``to_apic()``
  outputs — the most ordinary code there is.
* **Producers say exactly what they return** — ``JsonObject`` — so the caller
  narrows each step (``assert_type`` pins).
* **``AsyncNiwaki.gather``** is precise up to five coroutines and falls back
  to ``tuple[Any, ...]`` beyond (heterogeneous arity has no typed spelling).
* **``ApicSession.get(**params)``** takes scalars and sequences of scalars
  (httpx repeats the key for a list).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, assert_type

import httpx

from niwaki import AsyncNiwaki
from niwaki.design import Cursor, from_payload, tenant
from niwaki.models.base import ManagedObject
from niwaki.snapshot import Snapshot
from niwaki.transport import ApicSession, AsyncApicSession, AsyncMoWriter, MoWriter
from niwaki.types import JsonObject
from niwaki.utils import parse_imdata
from niwaki.utils.response import parse_envelopes

# ── consumers: realistic inputs type-check ───────────────────────────────────


def consumer_literals() -> None:
    # An unannotated JSON literal, flat and nested.
    raw = {"fvTenant": {"attributes": {"name": "prod", "dn": "uni/tn-prod"}}}
    ManagedObject.from_apic(raw)
    nested = {
        "fvTenant": {
            "attributes": {"name": "prod"},
            "children": [{"fvBD": {"attributes": {"name": "web"}}}],
        }
    }
    ManagedObject.from_apic(nested)
    # An unannotated imdata body.
    body = {"imdata": [{"fvTenant": {"attributes": {"name": "x"}}}], "totalCount": "1"}
    parse_imdata(body)
    parse_envelopes([{"fvTenant": {"attributes": {"name": "x"}}}])


def consumer_typed_inputs(
    any_payload: dict[str, Any],
    precise: dict[str, dict[str, dict[str, str]]],
    view: Mapping[str, object],
    rows: list[dict[str, str]],
) -> None:
    from_payload(any_payload)
    ManagedObject.from_apic(precise)
    ManagedObject.from_apic(view)
    for row in rows:
        ManagedObject.from_event({"fvTenant": {"attributes": row}})


def consumer_untyped_sources(resp: httpx.Response) -> None:
    parse_imdata(resp.json())
    parse_imdata(json.loads("{}"))


def consumer_writes(session: ApicSession, mo: ManagedObject) -> None:
    session.post_mo("uni/tn-x", {"fvTenant": {"attributes": {"name": "x"}}})
    payload = {"fvTenant": {"attributes": {"name": "x"}}}
    session.post_mo("uni/tn-x", payload)
    session.post_mo(mo.dn, mo.to_apic())


# ── producer → consumer round trips ──────────────────────────────────────────


def roundtrip(session: ApicSession) -> None:
    raw = session.get("/api/class/fvBD.json")
    parse_imdata({"imdata": raw})
    parse_envelopes(raw)
    ManagedObject.from_apic(raw[0])
    envelopes = [m.to_apic() for m in parse_imdata({"imdata": session.get("/api/class/fvBD.json")})]
    session.post_mo("uni", {"polUni": {"attributes": {}, "children": envelopes}})
    from_payload(tenant("x").to_payload())


async def roundtrip_async(session: AsyncApicSession) -> None:
    raw = await session.get("/api/class/fvBD.json")
    envelopes = [m.to_apic() for m in parse_imdata({"imdata": raw})]
    await session.post_mo("uni", {"polUni": {"attributes": {}, "children": envelopes}})


# ── writer protocols: the sessions and a Mapping-typed fake conform ──────────


class MappingWriter:
    """A user test double typed with the consumer contract."""

    def post_mo(self, dn: str, payload: Mapping[str, object]) -> None: ...

    def delete_mo(self, dn: str) -> None: ...


class AsyncMappingWriter:
    """The async twin of :class:`MappingWriter`."""

    async def post_mo(self, dn: str, payload: Mapping[str, object]) -> None: ...

    async def delete_mo(self, dn: str) -> None: ...


def writers(session: ApicSession, async_session: AsyncApicSession) -> None:
    writer: MoWriter = MappingWriter()
    writer = session
    async_writer: AsyncMoWriter = AsyncMappingWriter()
    async_writer = async_session
    del writer, async_writer


# ── producers: exact JsonObject ──────────────────────────────────────────────


def producers(mo: ManagedObject, design: Cursor, session: ApicSession, snapshot: Snapshot) -> None:
    # The APIC envelope of one object: {"fvTenant": {"attributes": ..., ...}}.
    assert_type(mo.to_apic(), JsonObject)
    # The polUni envelope a strict push POSTs to /api/mo/uni.json.
    assert_type(design.to_payload(), JsonObject)
    # The ``imdata`` list: one APIC envelope per item.
    assert_type(session.get("/api/mo/uni.json"), list[JsonObject])
    # The captured wire tree, None for an empty scope.
    assert_type(snapshot.tree, JsonObject | None)


async def producers_async(session: AsyncApicSession) -> None:
    assert_type(await session.get("/api/mo/uni.json"), list[JsonObject])


def snapshot_from_literal() -> Snapshot:
    # An inline literal is inferred against the declared JsonObject field.
    return Snapshot(
        scope="uni", tree={"class": "polUni", "rn": "uni", "attributes": {}, "children": []}
    )


# ── gather: precise to five, Any beyond ──────────────────────────────────────


async def gather(aci: AsyncNiwaki) -> None:
    async def number() -> int:
        return 1

    async def text() -> str:
        return ""

    assert_type(await aci.gather(number(), text()), tuple[int, str])
    six = await aci.gather(number(), text(), number(), text(), number(), text())
    assert_type(six, tuple[Any, ...])


# ── get(**params): scalars and sequences of scalars ──────────────────────────


def get_params(session: ApicSession) -> None:
    session.get("/api/class/fvBD.json", **{"query-target": "subtree"})
    session.get("/api/class/fvBD.json", page=0, order_by=["fvBD.name", "fvBD.dn"])
    session.get("/api/class/fvBD.json", ids=(1, 2, 3), flag=True)


async def get_params_async(session: AsyncApicSession) -> None:
    await session.get("/api/class/fvBD.json", order_by=["fvBD.name"], page=0)
