"""Tests for schema-derived metadata in generated ManagedObject subclasses.

Covers:
- ClassVar semantic flags (_mo_category, _write_access, _is_observable, etc.)
- Secure fields (repr=False prevents accidental log exposure)
- Create-only fields (separate section; present on model but flagged)
- ManagedObject base defaults (all flags have safe defaults)
- Pipeline integrity (metadata survives 01→02→03→generate round-trip)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic.fields import FieldInfo

from niwaki.models.base import ManagedObject

# ── Helpers ───────────────────────────────────────────────────────────────────

_SUBSET = Path(__file__).parent.parent.parent / "data" / "extracted" / "sdk_subset.json"


def _load_fvBD() -> type[ManagedObject]:
    from niwaki.models._generated.fv.fvBD import fvBD

    return fvBD


def _load_aaaLdapProvider() -> type[ManagedObject]:
    from niwaki.models._generated.aaa.aaaLdapProvider import aaaLdapProvider

    return aaaLdapProvider


def _load_aaaActiveUserSession() -> type[ManagedObject]:
    from niwaki.models._generated.aaa.aaaActiveUserSession import aaaActiveUserSession

    return aaaActiveUserSession


def _load_fvRsCtx() -> type[ManagedObject]:
    from niwaki.models._generated.fv.fvRsCtx import fvRsCtx

    return fvRsCtx


# ── ManagedObject base defaults ───────────────────────────────────────────────


class TestManagedObjectDefaults:
    """The base class must define safe defaults for all new ClassVars."""

    def test_mo_category_default(self) -> None:
        assert ManagedObject._mo_category == "Regular"  # type: ignore[reportPrivateUsage]

    def test_write_access_default_empty(self) -> None:
        assert ManagedObject._write_access == frozenset()  # type: ignore[reportPrivateUsage]

    def test_is_observable_default_false(self) -> None:
        assert ManagedObject._is_observable is False  # type: ignore[reportPrivateUsage]

    def test_is_faultable_default_false(self) -> None:
        assert ManagedObject._is_faultable is False  # type: ignore[reportPrivateUsage]

    def test_is_health_scorable_default_false(self) -> None:
        assert ManagedObject._is_health_scorable is False  # type: ignore[reportPrivateUsage]

    def test_has_stats_default_false(self) -> None:
        assert ManagedObject._has_stats is False  # type: ignore[reportPrivateUsage]

    def test_all_classvars_are_classvars(self) -> None:
        """New flags must not appear as Pydantic model fields."""
        model_fields = set(ManagedObject.model_fields)
        for attr in (
            "_mo_category",
            "_write_access",
            "_is_observable",
            "_is_faultable",
            "_is_health_scorable",
            "_has_stats",
        ):
            assert attr not in model_fields, f"{attr!r} must be ClassVar, not a Pydantic field"


# ── fvBD — canonical policy object ───────────────────────────────────────────


class TestFvBDMetadata:
    """fvBD is a Regular, Observable, Faultable, HealthScorable class with stats."""

    def test_mo_category_regular(self) -> None:
        assert _load_fvBD()._mo_category == "Regular"  # type: ignore[reportPrivateUsage]

    def test_write_access_contains_admin(self) -> None:
        wa = _load_fvBD()._write_access  # type: ignore[reportPrivateUsage]
        assert isinstance(wa, frozenset)
        assert "admin" in wa

    def test_write_access_contains_tenant_connectivity(self) -> None:
        assert "tenant-connectivity" in _load_fvBD()._write_access  # type: ignore[reportPrivateUsage]

    def test_is_observable_true(self) -> None:
        assert _load_fvBD()._is_observable is True  # type: ignore[reportPrivateUsage]

    def test_is_faultable_true(self) -> None:
        assert _load_fvBD()._is_faultable is True  # type: ignore[reportPrivateUsage]

    def test_is_health_scorable_true(self) -> None:
        assert _load_fvBD()._is_health_scorable is True  # type: ignore[reportPrivateUsage]

    def test_has_stats_true(self) -> None:
        assert _load_fvBD()._has_stats is True  # type: ignore[reportPrivateUsage]

    def test_docstring_contains_label(self) -> None:
        assert "Bridge Domain" in (_load_fvBD().__doc__ or "")


# ── Rs class — RelationshipToLocal ───────────────────────────────────────────


class TestFvRsCtxMetadata:
    """Rs classes are RelationshipToLocal; they have a distinct mo_category."""

    def test_mo_category_is_relationship(self) -> None:
        cls = _load_fvRsCtx()
        assert cls._mo_category == "RelationshipToLocal"  # type: ignore[reportPrivateUsage]

    def test_write_access_populated(self) -> None:
        cls = _load_fvRsCtx()
        assert isinstance(cls._write_access, frozenset)  # type: ignore[reportPrivateUsage]
        assert len(cls._write_access) > 0  # type: ignore[reportPrivateUsage]


# ── Secure fields — repr=False prevents log exposure ─────────────────────────


class TestSecureFields:
    """Password/key fields must not appear in repr() output."""

    def test_key_field_not_in_repr(self) -> None:
        cls = _load_aaaLdapProvider()
        fi: FieldInfo | None = cls.model_fields.get("password")
        assert fi is not None, "aaaLdapProvider.password (alias 'key') must be a model field"
        assert fi.repr is False, "secure field 'password' must have repr=False"

    def test_monitoring_password_not_in_repr(self) -> None:
        cls = _load_aaaLdapProvider()
        fi: FieldInfo | None = cls.model_fields.get("periodic_server_monitoring_password")
        assert fi is not None
        assert fi.repr is False

    def test_secure_field_absent_from_instance_repr(self) -> None:
        """Instance repr must omit secure fields entirely."""
        cls = _load_aaaLdapProvider()
        # aaaLdapProvider naming prop is 'name'; 'key' is the alias for 'password'
        instance = cls(name="ldap-test", password="s3cr3t")
        r = repr(instance)
        assert "s3cr3t" not in r, "secret value must not appear in repr"
        assert "password" not in r, "secure field name must not appear in repr"

    def test_secure_field_still_accessible(self) -> None:
        """repr=False only suppresses repr, not attribute access."""
        cls = _load_aaaLdapProvider()
        instance = cls(name="ldap-test", password="s3cr3t")
        assert instance.password == "s3cr3t"  # type: ignore[attr-defined]

    def test_secure_field_included_in_to_apic_when_set(self) -> None:
        """Secure fields are still serialised by to_apic() when explicitly set."""
        cls = _load_aaaLdapProvider()
        instance = cls(name="ldap-test", password="s3cr3t")
        payload = instance.to_apic()
        attrs = payload["aaaLdapProvider"]["attributes"]
        assert attrs["key"] == "s3cr3t"

    def test_non_secure_field_appears_in_repr(self) -> None:
        """Sanity: non-secure fields ARE shown in repr."""
        cls = _load_aaaLdapProvider()
        fi = cls.model_fields.get("port")
        if fi is not None:
            assert fi.repr is not False


# ── Create-only fields ────────────────────────────────────────────────────────


class TestCreateOnlyFields:
    """Create-only fields are present on the model but belong to a separate section."""

    def test_create_only_name_is_a_model_field(self) -> None:
        """aaaActiveUserSession.name is create_only, not naming — must be a model field."""
        cls = _load_aaaActiveUserSession()
        assert "name" in cls.model_fields

    def test_naming_prop_is_hashToken_not_name(self) -> None:
        """The actual naming prop for aaaActiveUserSession is hashToken (→ token_identifier)."""
        cls = _load_aaaActiveUserSession()
        assert cls._naming_props == ["token_identifier"]  # type: ignore[reportPrivateUsage]
        assert "token_identifier" in cls.model_fields

    def test_create_only_field_has_default(self) -> None:
        """Create-only fields have a default value (they're optional on construction)."""
        cls = _load_aaaActiveUserSession()
        fi = cls.model_fields.get("name")
        assert fi is not None
        # Has a default (empty string or similar)
        assert fi.default is not None or fi.default_factory is not None or fi.default == ""

    def test_create_only_field_sent_in_to_apic_when_set(self) -> None:
        cls = _load_aaaActiveUserSession()
        instance = cls(hashToken="abc123", name="my-session")  # type: ignore[call-arg]
        payload = instance.to_apic()
        attrs = payload["aaaActiveUserSession"]["attributes"]
        assert attrs["hashToken"] == "abc123"
        assert attrs["name"] == "my-session"

    def test_create_only_field_not_sent_when_not_set(self) -> None:
        """If create_only field is not provided, to_apic() does not include it."""
        cls = _load_aaaActiveUserSession()
        instance = cls(hashToken="abc123")  # type: ignore[call-arg]
        payload = instance.to_apic()
        attrs = payload["aaaActiveUserSession"]["attributes"]
        # name is not in model_fields_set → not serialised
        assert "name" not in attrs


# ── Pipeline integrity — metadata comes from sdk_subset.json ─────────────────


@pytest.mark.skipif(
    not _SUBSET.exists(),
    reason="requires the extracted APIC schema data (data/extracted/, not in the repository)",
)
class TestPipelineIntegrity:
    """Generated metadata must match what's in the extraction output."""

    @pytest.fixture(scope="class")
    @classmethod
    def subset(cls) -> dict[str, Any]:
        return json.loads(_SUBSET.read_text())

    def test_fvbd_mo_category_matches_subset(self, subset: dict) -> None:
        expected = subset["fvBD"]["class"]["mo_category"]
        assert _load_fvBD()._mo_category == expected  # type: ignore[reportPrivateUsage]

    def test_fvbd_write_access_matches_subset(self, subset: dict) -> None:
        expected = frozenset(subset["fvBD"]["class"]["write_access"])
        assert _load_fvBD()._write_access == expected  # type: ignore[reportPrivateUsage]

    def test_fvbd_is_observable_matches_subset(self, subset: dict) -> None:
        expected = subset["fvBD"]["class"]["is_observable"]
        assert _load_fvBD()._is_observable is expected  # type: ignore[reportPrivateUsage]

    def test_fvbd_is_faultable_matches_subset(self, subset: dict) -> None:
        expected = subset["fvBD"]["class"]["is_faultable"]
        assert _load_fvBD()._is_faultable is expected  # type: ignore[reportPrivateUsage]

    def test_fvbd_has_stats_matches_subset(self, subset: dict) -> None:
        expected = subset["fvBD"]["class"]["has_stats"]
        assert _load_fvBD()._has_stats is expected  # type: ignore[reportPrivateUsage]

    def test_all_classes_have_mo_category(self, subset: dict) -> None:
        """Every entry in sdk_subset must have mo_category after running 01."""
        missing = [n for n, d in subset.items() if "mo_category" not in d["class"]]
        assert missing == [], f"Missing mo_category: {missing[:5]}"

    def test_all_classes_have_write_access(self, subset: dict) -> None:
        missing = [n for n, d in subset.items() if "write_access" not in d["class"]]
        assert missing == [], f"Missing write_access: {missing[:5]}"

    def test_secure_flag_present_in_subset_for_key_prop(self, subset: dict) -> None:
        props = subset.get("aaaLdapProvider", {}).get("properties", {})
        assert props.get("key", {}).get("secure") is True

    def test_create_only_flag_present_in_subset(self, subset: dict) -> None:
        props = subset.get("aaaActiveUserSession", {}).get("properties", {})
        assert props.get("name", {}).get("create_only") is True


# ── Cisco schema comments piped into the generated surface ───────────────────


class TestCiscoDescriptions:
    """The schema ``comment`` flows into docstrings and Field descriptions."""

    def test_class_docstring_carries_cisco_definition(self) -> None:
        doc = _load_fvBD().__doc__ or ""
        assert "unique layer 2 forwarding domain" in doc

    def test_field_description_carries_cisco_definition(self) -> None:
        info = _load_fvBD().model_fields["arp_flooding"]
        assert info.description is not None
        assert "ARP flooding" in info.description

    def test_enum_field_description(self) -> None:
        from niwaki.models._generated.ospf.ospfIfPol import ospfIfPol

        info = ospfIfPol.model_fields["network_type"]
        assert info.description is not None
        assert "point-to-point and broadcast" in info.description

    def test_enum_value_docstrings_in_source(self) -> None:
        # Attribute docstrings are a static convention (read by IDEs and
        # Sphinx, not stored at runtime) — assert on the generated source.
        import inspect

        from niwaki.models._generated.enums.OspfNwT import OspfNwT

        source = inspect.getsource(OspfNwT)
        assert 'BCAST = "bcast"\n    """Broadcast interface"""' in source

    def test_field_without_comment_has_no_description(self) -> None:
        # fvBD.ipLearning carries no schema comment — stays undescribed.
        assert _load_fvBD().model_fields["ip_learning"].description is None
