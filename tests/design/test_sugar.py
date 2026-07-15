"""Parameter sugar — vzEntry port shorthand and vzBrCP scope translation."""

from __future__ import annotations

import pytest

from niwaki.design import tenant
from niwaki.design._sugar import apply_sugar
from niwaki.exceptions import DesignError


class TestEntrySugar:
    def test_tcp_int(self) -> None:
        entry = tenant("t").filter("web").entry("http", tcp=80)
        assert entry.design_node.attrs == {
            "ethernet_type": "ip",
            "protocol": "tcp",
            "destination_from_port": 80,
            "destination_to_port": 80,
        }

    def test_tcp_tuple_range(self) -> None:
        attrs = apply_sugar("vzEntry", {"tcp": (8000, 8090)})
        assert attrs["destination_from_port"] == 8000
        assert attrs["destination_to_port"] == 8090

    def test_tcp_dashed_string_range(self) -> None:
        attrs = apply_sugar("vzEntry", {"tcp": "8000-8090"})
        assert attrs["destination_from_port"] == "8000"
        assert attrs["destination_to_port"] == "8090"

    def test_tcp_named_port(self) -> None:
        attrs = apply_sugar("vzEntry", {"tcp": "https"})
        assert attrs["destination_from_port"] == "https"
        assert attrs["destination_to_port"] == "https"

    def test_udp(self) -> None:
        attrs = apply_sugar("vzEntry", {"udp": 53})
        assert attrs["protocol"] == "udp"
        # The sugar passes the port through as written; the model owns the rest
        # (53 has a name in the schema — the APIC stores it as "dns").
        assert attrs["destination_from_port"] == 53

    def test_icmp_implies_ip(self) -> None:
        attrs = apply_sugar("vzEntry", {"protocol": "icmp"})
        assert attrs == {"protocol": "icmp", "ethernet_type": "ip"}

    def test_explicit_fields_not_overridden(self) -> None:
        attrs = apply_sugar("vzEntry", {"tcp": 80, "destination_to_port": "8080"})
        assert attrs["destination_to_port"] == "8080"

    def test_tcp_and_udp_together_raises(self) -> None:
        with pytest.raises(DesignError, match="not both"):
            apply_sugar("vzEntry", {"tcp": 80, "udp": 53})

    def test_serialises_to_wire_names(self) -> None:
        payload = tenant("t").filter("web").entry("http", tcp=80).design_node.mo().to_apic()
        attrs = payload["vzEntry"]["attributes"]
        assert attrs["etherT"] == "ip"
        assert attrs["prot"] == "tcp"
        # 80 has a name in the ACI schema, and the APIC stores it under that name
        # — a port pushed as 80 is read back as "http".  The SDK stores what the
        # APIC stores, so the two never disagree.
        assert attrs["dFromPort"] == "http"
        assert attrs["dToPort"] == "http"

    def test_a_port_without_a_name_stays_a_number(self) -> None:
        payload = tenant("t").filter("web").entry("api", tcp=8080).design_node.mo().to_apic()
        attrs = payload["vzEntry"]["attributes"]
        assert attrs["dFromPort"] == "8080"

    def test_the_name_may_be_written_directly(self) -> None:
        payload = tenant("t").filter("web").entry("http", tcp="http").design_node.mo().to_apic()
        assert payload["vzEntry"]["attributes"]["dFromPort"] == "http"


class TestPortSpecErrors:
    def test_unsupported_port_type_raises(self) -> None:
        with pytest.raises(DesignError, match="Unsupported port specification"):
            apply_sugar("vzEntry", {"tcp": 1.5})


class TestContractScopeSugar:
    def test_scope_vrf_maps_to_context(self) -> None:
        contract = tenant("t").contract("c").set(scope="vrf")
        assert contract.design_node.attrs == {"scope": "context"}

    def test_other_scopes_untouched(self) -> None:
        contract = tenant("t").contract("c").set(scope="tenant")
        assert contract.design_node.attrs == {"scope": "tenant"}


class TestSugarPassthrough:
    def test_non_sugared_class_untouched(self) -> None:
        attrs = {"unicast_routing": True}
        assert apply_sugar("fvBD", attrs) is attrs
