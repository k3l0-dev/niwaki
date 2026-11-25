"""The design DSL already refuses a name on a fixed-RN maker — guard it stays so.

Companion of ``tests/facade/test_fixed_rn_navigation.py`` (the facade
silently dropped a name passed to a fixed-RN edge).  The DSL never had that
defect; this module pins why, so a refactor cannot introduce it:

* **Typed cursors** (``design/_generated_cursors``): a fixed-RN maker is
  generated keyword-only (``def pim(self, *, name=None, …)``) — a positional
  name is a Python :class:`TypeError` at the call.
* **Dynamic cursors** (``Cursor.__getattr__``, e.g. a cursor built over a
  bare design node): positional arguments map onto the child's naming props
  (``_split_naming``) and a fixed-RN child has none — a positional name
  raises :class:`~niwaki.exceptions.DesignError`.
* **A keyword ``name=`` is not swallowed**: on a fixed-RN child, ``name`` is
  an ordinary attribute (``pimBDP.name``), declared and pushed like any other
  — it lands in the payload, it never names the object.
* **No Rs maker exists** — relations are declared with ``bind()``/verbs, so
  the facade's Rs-singleton case has no DSL counterpart.  ``custom_qos_policy``
  reached from an EPG is the *tenant-level* QoS policy maker (named, found by
  the ancestor walk): ``"gold"`` names that policy, nothing is dropped.
"""

from __future__ import annotations

import importlib
import re
from functools import cache
from typing import TYPE_CHECKING, cast

import pytest

from niwaki.design import Cursor, design, tenant
from niwaki.design._cursor import _split_naming, _tables  # pyright: ignore[reportPrivateUsage]
from niwaki.domain._child_map import CLASS_PKG, RS_TARGET_PROP
from niwaki.exceptions import DesignError

if TYPE_CHECKING:
    from collections.abc import Callable

    from niwaki.models.base import ManagedObject


@cache
def _model(aci_class: str) -> type[ManagedObject]:
    module = importlib.import_module(f"niwaki.models._generated.{CLASS_PKG[aci_class]}.{aci_class}")
    return cast("type[ManagedObject]", getattr(module, aci_class))


@cache
def _fixed_rn_makers() -> tuple[tuple[str, str, str], ...]:
    """Every curated ``(parent, maker, child)`` whose child has no naming prop."""
    return tuple(
        (parent, maker, child)
        for parent, row in sorted(_tables().makers.items())
        for maker, child in sorted(row.items())
        if not _model(child)._naming_props  # pyright: ignore[reportPrivateUsage]
    )


def test_fixed_rn_makers_exist_and_none_is_an_rs() -> None:
    makers = _fixed_rn_makers()
    assert len(makers) > 100, "the fixed-RN maker sweep was thinned out"
    assert ("fvBD", "pim", "pimBDP") in makers
    relations = [m for m in makers if m[2] in RS_TARGET_PROP or re.match(r"[a-z0-9]+Rs[A-Z]", m[2])]
    assert relations == []


def test_every_fixed_rn_maker_refuses_a_positional_name() -> None:
    """The gate every dynamic maker goes through refuses any positional name."""
    accepted: list[str] = []
    for parent, maker, child in _fixed_rn_makers():
        try:
            _split_naming(_model(child), ("gold",), {})
        except DesignError as error:
            assert "takes at most 0 naming argument" in str(error)
            continue
        accepted.append(f"{parent}.{maker} → {child}")
    assert accepted == []


TYPED_CASES: dict[str, Callable[[], object]] = {
    "root-infra": lambda: design().infra("x"),  # type: ignore[call-arg]
    "tenant-endpoint-tags": lambda: tenant("t").endpoint_tags("x"),  # type: ignore[call-arg]
    "bd-pim": lambda: tenant("t").bd("b").pim("gold"),  # type: ignore[call-arg]
    "epg-criterion": lambda: tenant("t").app("a").epg("w").criterion("x"),  # type: ignore[call-arg]
}


@pytest.mark.parametrize("call", TYPED_CASES.values(), ids=TYPED_CASES.keys())
def test_typed_fixed_rn_maker_refuses_a_positional_name(call: Callable[[], object]) -> None:
    with pytest.raises(TypeError, match="positional argument"):
        call()


DYNAMIC_CASES: dict[str, tuple[Callable[[], Cursor], str]] = {
    "bd-pim": (lambda: Cursor(tenant("t").bd("b").design_node), "pim"),
    "epg-criterion": (lambda: Cursor(tenant("t").app("a").epg("w").design_node), "criterion"),
    "tenant-endpoint-tags": (lambda: Cursor(tenant("t").design_node), "endpoint_tags"),
}


@pytest.mark.parametrize(("parent", "maker"), DYNAMIC_CASES.values(), ids=DYNAMIC_CASES.keys())
def test_dynamic_fixed_rn_maker_refuses_a_positional_name(
    parent: Callable[[], Cursor], maker: str
) -> None:
    with pytest.raises(DesignError, match="takes at most 0 naming argument"):
        getattr(parent(), maker)("gold")


def test_fixed_rn_maker_without_argument_keeps_its_fixed_rn() -> None:
    assert tenant("t").bd("b").pim().dn == "uni/tn-t/BD-b/pimbdp"
    assert Cursor(tenant("t").bd("b").design_node).pim().dn == "uni/tn-t/BD-b/pimbdp"


def test_keyword_name_on_a_fixed_rn_maker_is_an_attribute_not_a_name() -> None:
    """``name=`` is declared config: same DN, and it reaches the payload."""
    pim = tenant("t").bd("b").pim(name="gold")
    assert pim.dn == "uni/tn-t/BD-b/pimbdp"
    assert pim.design_node.attrs == {"name": "gold"}
    assert pim.design_node.mo().to_apic() == {"pimBDP": {"attributes": {"name": "gold"}}}


def test_custom_qos_policy_under_an_epg_names_the_tenant_policy() -> None:
    """The DSL counterpart of the facade defect: ``"gold"`` is used, not dropped."""
    policy = tenant("t").app("a").epg("w").custom_qos_policy("gold")
    assert policy.dn == "uni/tn-t/qoscustom-gold"
    assert policy.design_node.aci_class == "qosCustomPol"
