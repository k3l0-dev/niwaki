"""mo_diff respect_fields_set — only explicitly set fields are compared."""

from __future__ import annotations

from typing import cast

from niwaki.models._generated.fv.fvBD import fvBD
from niwaki.models._generated.fv.fvSubnet import fvSubnet
from niwaki.models.base import ManagedObject
from niwaki.utils.diff import mo_diff


def _current_bd(**wire_attrs: str) -> fvBD:
    """A BD as it would come back from the APIC (wire names).

    ``from_apic`` dispatches on the envelope's class name, so it is typed to the
    base class; the envelope here says ``fvBD``.
    """
    return cast(
        fvBD,
        ManagedObject.from_apic(
            {"fvBD": {"attributes": {"name": "web", **wire_attrs}, "children": []}}
        ),
    )


class TestRespectFieldsSet:
    def test_untouched_field_ignored(self) -> None:
        """Desired never set arpFlood — the APIC value must not diff."""
        desired = fvBD(name="web")
        current = _current_bd(arpFlood="yes")  # differs from schema default
        assert mo_diff(desired, current, respect_fields_set=True) is None  # type: ignore[arg-type]

    def test_default_behaviour_unchanged(self) -> None:
        """Without the flag, the original all-fields comparison still applies."""
        desired = fvBD(name="web")
        current = _current_bd(arpFlood="yes")
        delta = mo_diff(desired, current)
        assert delta is not None
        assert delta.arp_flooding is False

    def test_set_field_still_detected(self) -> None:
        desired = fvBD(name="web", unicast_routing=True)
        current = _current_bd(unicastRoute="no")
        delta = mo_diff(desired, current, respect_fields_set=True)  # type: ignore[arg-type]
        assert delta is not None
        assert delta.to_apic()["fvBD"]["attributes"] == {
            "name": "web",
            "unicastRoute": "true",
        }

    def test_set_field_matching_current_yields_none(self) -> None:
        desired = fvBD(name="web", unicast_routing=True)
        current = _current_bd(unicastRoute="yes")
        assert mo_diff(desired, current, respect_fields_set=True) is None  # type: ignore[arg-type]

    def test_propagates_to_children(self) -> None:
        desired = fvBD(name="web")
        desired.children.append(fvSubnet(subnet="10.0.1.1/24"))
        current = ManagedObject.from_apic(
            {
                "fvBD": {
                    "attributes": {"name": "web"},
                    "children": [
                        {
                            "fvSubnet": {
                                # 'preferred' differs from the schema default but
                                # was never set on the desired subnet.
                                "attributes": {"ip": "10.0.1.1/24", "preferred": "yes"},
                            }
                        }
                    ],
                }
            }
        )
        assert mo_diff(desired, current, respect_fields_set=True) is None  # type: ignore[arg-type]


class TestNumericNormalisation:
    """The APIC canonicalises numbers on write — and the SDK types them as numbers.

    ``stormctrlIfPol.bcRate`` is a ``scalar:Float``.  While the SDK typed it as a
    string, an ``"80.0"`` pushed and an ``"80.000000"`` read back were two
    different strings, and the diff had to reparse both sides as floats to avoid
    reporting a change that had not happened.  Typed as a ``float``, the APIC's
    spelling stops being a fact anyone has to know.
    """

    def test_apic_normalised_float_is_not_a_change(self) -> None:
        from niwaki.models._generated.stormctrl.stormctrlIfPol import stormctrlIfPol

        desired = stormctrlIfPol(name="s", broadcast_traffic_rate=80.0)
        current = stormctrlIfPol.model_validate({"name": "s", "bcRate": "80.000000"})
        assert desired.broadcast_traffic_rate == 80.0
        assert mo_diff(desired, current, respect_fields_set=True) is None

    def test_the_string_spelling_is_accepted_too(self) -> None:
        """A caller who writes the wire form is not punished for it."""
        from niwaki.models._generated.stormctrl.stormctrlIfPol import stormctrlIfPol

        desired = stormctrlIfPol(name="s", broadcast_traffic_rate=80.0)
        current = stormctrlIfPol.model_validate({"name": "s", "bcRate": "80.000000"})
        assert mo_diff(desired, current, respect_fields_set=True) is None

    def test_real_numeric_change_is_reported(self) -> None:
        from niwaki.models._generated.stormctrl.stormctrlIfPol import stormctrlIfPol

        desired = stormctrlIfPol(name="s", broadcast_traffic_rate=90.0)
        current = stormctrlIfPol.model_validate({"name": "s", "bcRate": "80.000000"})
        delta = mo_diff(desired, current, respect_fields_set=True)
        assert delta is not None
        assert delta.broadcast_traffic_rate == 90.0

    def test_non_numeric_strings_compare_strictly(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        desired = fvBD(name="web", description="1.0-release")
        current = fvBD.model_validate({"name": "web", "descr": "1.00-release"})
        assert mo_diff(desired, current, respect_fields_set=True) is not None
