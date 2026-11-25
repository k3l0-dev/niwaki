"""Shared fixtures for the ``niwaki.testing`` contract suite.

Every test here talks to :class:`niwaki.testing.FakeApic` through the SDK
itself (``fake.client()``, a raw :class:`~niwaki.transport.ApicSession`
wired on ``fake.transport``) or, for wire-level assertions, through
``fake.handle``.  Nothing reaches into the fake's private state: the store is
observed through ``fake.get`` / ``fake.objects`` only.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING

import httpx
import pytest

from niwaki import Niwaki
from niwaki.transport import ApicSession
from niwaki.types import JsonObject, JsonValue

from .._jsonpath import as_object, as_objects, at

if TYPE_CHECKING:
    from niwaki.testing import FakeApic

#: A small fabric: two tenants, bridge domains with subnets and relations,
#: an application profile whose EPG carries a static path (a nested-bracket RN).
FABRIC: JsonObject = {
    "polUni": {
        "attributes": {},
        "children": [
            {
                "fvTenant": {
                    "attributes": {"name": "prod"},
                    "children": [
                        {
                            "fvBD": {
                                "attributes": {
                                    "name": "web",
                                    "arpFlood": "yes",
                                    "descr": 'say "hi"',
                                },
                                "children": [
                                    {
                                        "fvSubnet": {
                                            "attributes": {
                                                "ip": "10.0.1.1/24",
                                                "scope": "public,shared",
                                            }
                                        }
                                    },
                                    {
                                        "fvSubnet": {
                                            "attributes": {"ip": "10.0.2.1/24", "scope": "private"}
                                        }
                                    },
                                    {"fvRsCtx": {"attributes": {"tnFvCtxName": "main"}}},
                                ],
                            }
                        },
                        {
                            "fvBD": {
                                "attributes": {"name": "db", "arpFlood": "no", "descr": "a\\b"},
                                "children": [
                                    {
                                        "fvSubnet": {
                                            "attributes": {"ip": "10.0.3.1/24", "scope": "public"}
                                        }
                                    }
                                ],
                            }
                        },
                        {"fvCtx": {"attributes": {"name": "main"}}},
                        {
                            "fvAp": {
                                "attributes": {"name": "shop"},
                                "children": [
                                    {
                                        "fvAEPg": {
                                            "attributes": {"name": "web"},
                                            "children": [
                                                {"fvRsBd": {"attributes": {"tnFvBDName": "web"}}},
                                                {
                                                    "fvRsPathAtt": {
                                                        "attributes": {
                                                            "tDn": (
                                                                "topology/pod-1/paths-101/"
                                                                "pathep-[eth1/1]"
                                                            ),
                                                            "encap": "vlan-10",
                                                        }
                                                    }
                                                },
                                            ],
                                        }
                                    }
                                ],
                            }
                        },
                    ],
                }
            },
            {
                "fvTenant": {
                    "attributes": {"name": "dev"},
                    "children": [{"fvBD": {"attributes": {"name": "web", "arpFlood": "no"}}}],
                }
            },
        ],
    }
}

#: Every DN :data:`FABRIC` materialises.
FABRIC_DNS = frozenset(
    {
        "uni",
        "uni/tn-prod",
        "uni/tn-prod/BD-web",
        "uni/tn-prod/BD-web/subnet-[10.0.1.1/24]",
        "uni/tn-prod/BD-web/subnet-[10.0.2.1/24]",
        "uni/tn-prod/BD-web/rsctx",
        "uni/tn-prod/BD-db",
        "uni/tn-prod/BD-db/subnet-[10.0.3.1/24]",
        "uni/tn-prod/ctx-main",
        "uni/tn-prod/ap-shop",
        "uni/tn-prod/ap-shop/epg-web",
        "uni/tn-prod/ap-shop/epg-web/rsbd",
        "uni/tn-prod/ap-shop/epg-web/rspathAtt-[topology/pod-1/paths-101/pathep-[eth1/1]]",
        "uni/tn-dev",
        "uni/tn-dev/BD-web",
    }
)

HOST = "https://apic.example.com"


@pytest.fixture
def fake() -> FakeApic:
    """A fresh, empty fake APIC."""
    from niwaki.testing import FakeApic

    return FakeApic()


@pytest.fixture
def seeded(fake: FakeApic) -> FakeApic:
    """The :data:`FABRIC` seeded into a fresh fake."""
    fake.seed(FABRIC)
    return fake


@pytest.fixture
def aci(fake: FakeApic) -> Iterator[Niwaki]:
    """A connected sync client on the fake (seed through ``seeded`` first if needed)."""
    with fake.client() as client:
        yield client


@pytest.fixture
def session(fake: FakeApic) -> Iterator[ApicSession]:
    """A logged-in raw transport session on ``fake.transport`` — the wire-level view."""
    http = httpx.Client(base_url=fake.host, transport=fake.transport)
    with ApicSession.with_client(http, fake.host, "admin", "password") as raw:
        yield raw


# ── Wire helpers ──────────────────────────────────────────────────────────────


def request(method: str, path: str, body: Mapping[str, object] | None = None) -> httpx.Request:
    """An httpx request against :data:`HOST` (a host the fake was NOT built with)."""
    content = json.dumps(body).encode() if body is not None else None
    return httpx.Request(method, f"{HOST}{path}", content=content)


def envelope(response: httpx.Response) -> JsonObject:
    """The parsed JSON body of a fake response."""
    return as_object(json.loads(response.content))


def imdata(response: httpx.Response) -> list[JsonObject]:
    """The ``imdata`` items of a fake response."""
    return as_objects(envelope(response)["imdata"])


def class_of(item: JsonObject) -> str:
    """The single class key of an APIC envelope item."""
    (cls,) = item.keys()
    return cls


def attrs_of(item: JsonObject) -> JsonObject:
    """The attributes of an APIC envelope item."""
    return as_object(at(item, class_of(item), "attributes"))


def children_of(item: JsonObject) -> list[JsonObject]:
    """The embedded children of an APIC envelope item (``[]`` when none)."""
    body = as_object(item[class_of(item)])
    raw: JsonValue = body.get("children", [])
    return as_objects(raw)


def dns_of(items: list[JsonObject]) -> list[str]:
    """The materialised ``dn`` of every item, in response order."""
    dns: list[str] = []
    for item in items:
        dn = attrs_of(item)["dn"]
        assert isinstance(dn, str)
        dns.append(dn)
    return dns


def error_text(response: httpx.Response) -> str:
    """``error.attributes.text`` of an APIC error envelope."""
    (item,) = imdata(response)
    assert class_of(item) == "error"
    text = attrs_of(item)["text"]
    assert isinstance(text, str)
    return text
