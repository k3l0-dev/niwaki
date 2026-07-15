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
from typing import TYPE_CHECKING, Any

import pytest
from pydantic.fields import FieldInfo

from niwaki.models.base import ManagedObject

if TYPE_CHECKING:
    from niwaki.models._generated.aaa.aaaLdapProvider import aaaLdapProvider

# ── Helpers ───────────────────────────────────────────────────────────────────

_SUBSET = Path(__file__).parent.parent.parent / "data" / "extracted" / "sdk_subset.json"


def _load_fvBD() -> type[ManagedObject]:
    from niwaki.models._generated.fv.fvBD import fvBD

    return fvBD


def _load_aaaLdapProvider() -> type[aaaLdapProvider]:
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
        assert instance.password == "s3cr3t"

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


# ── configIssues catalog — the APIC's declared inconsistency channel ──────────

_SCHEMAS_DIR = _SUBSET.parent.parent / "schemas" / "mo-apic-v6.0_9c"


class TestConfigIssuesCatalog:
    """The models carry the APIC's declared accepted-but-inconsistent states."""

    def test_fvbd_carries_its_declared_issues(self) -> None:
        issues = _load_fvBD()._config_issues
        assert "FHS-enabled-on-l2-only-bd" in issues
        assert "bd-cannot-combine-hardware-proxy-and-flood-in-encapsulation" in issues

    def test_healthy_markers_are_data_not_filtered(self) -> None:
        # Display filters "ok"; the machine catalog keeps the full enum.
        assert "ok" in _load_fvBD()._config_issues

    def test_class_without_channel_is_empty(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant

        assert fvTenant._config_issues == {}


@pytest.mark.skipif(
    not _SCHEMAS_DIR.exists(),
    reason="requires the raw APIC schemas (data/schemas/, not in the repository)",
)
class TestConfigIssuesIntegrity:
    """Anti-drift: the catalogs match the RAW schemas, class by class.

    The predicate and the extraction are re-stated here independently from
    ``data/scripts/01_extract_classes.py`` — if either side changes alone,
    this suite fails.
    """

    @staticmethod
    def _is_config_issues_prop(name: str) -> bool:
        lowered = name.lower()
        return "configissues" in lowered or lowered == "confissues"

    @classmethod
    def _schema_catalog(cls, aci_class: str) -> dict[str, list[str]]:
        """Raw ``{code: comment-list}`` straight from the schema file."""
        raw = json.loads((_SCHEMAS_DIR / f"{aci_class}.json").read_text())
        entity = next(iter(raw.values()))
        catalog: dict[str, list[str]] = {}
        for prop_name, prop in entity.get("properties", {}).items():
            if not isinstance(prop, dict) or not cls._is_config_issues_prop(prop_name):
                continue
            for entry in prop.get("validValues", []) or []:
                code = entry.get("localName", "")
                if code and code != "defaultValue":
                    comment = entry.get("comment") or []
                    label = (entry.get("label") or "").strip()
                    catalog.setdefault(code, comment if comment else [label])
        return catalog

    def test_every_class_matches_the_raw_schema(self) -> None:
        from importlib import import_module

        from niwaki.models._generated import _PKG_MAP

        classes_with_catalog = 0
        total_codes = 0
        for aci_class, pkg in _PKG_MAP.items():
            expected = self._schema_catalog(aci_class)
            model = getattr(import_module(f"niwaki.models._generated.{pkg}.{aci_class}"), aci_class)
            got: dict[str, str] = model._config_issues
            assert set(got) == set(expected), f"{aci_class}: code sets differ"
            placeholders = {"null", "none", "na", "n/a", "tbd", "todo"}
            for code, text_list in expected.items():
                raw_text = " ".join(" ".join(text_list).split())
                if raw_text.lower().rstrip(".") in placeholders:
                    raw_text = ""  # the cleaning contract drops placeholder texts
                if raw_text:
                    assert got[code], f"{aci_class}.{code}: schema description dropped"
                    assert got[code][:80] == raw_text[:80], f"{aci_class}.{code}: drift"
                else:
                    assert got[code] == "", f"{aci_class}.{code}: invented description"
            if expected:
                classes_with_catalog += 1
                total_codes += len(expected)

        # Coverage floors — a silent capture regression trips these.
        assert classes_with_catalog >= 90, classes_with_catalog
        assert total_codes >= 2000, total_codes


# ── Fault codes & relation info — declared constraint channels ───────────────


class TestStaticConstraintCatalogs:
    """Spot checks on the committed catalogs (run everywhere)."""

    def test_fvbd_fault_codes(self) -> None:
        codes = _load_fvBD()._fault_codes
        assert codes.get("F2305") == "fltFvBDMulticastEnabledOnL2BD"

    def test_rs_relation_info(self) -> None:
        from niwaki.models._generated.fv.fvRsCtx import fvRsCtx

        info = fvRsCtx._relation_info
        assert info["cardinality"] == "n-to-1"
        assert info["enforceable"] is True
        assert info["resolvable"] is True

    def test_non_rs_class_has_no_relation_info(self) -> None:
        assert _load_fvBD()._relation_info == {}


@pytest.mark.skipif(
    not _SCHEMAS_DIR.exists(),
    reason="requires the raw APIC schemas (data/schemas/, not in the repository)",
)
class TestStaticCatalogsIntegrity:
    """Anti-drift: fault codes and relation info match the RAW schemas."""

    def test_every_class_matches_the_raw_schema(self) -> None:
        from importlib import import_module

        from niwaki.models._generated import _PKG_MAP

        classes_with_faults = total_codes = classes_with_relation = 0
        for aci_class, pkg in _PKG_MAP.items():
            raw = json.loads((_SCHEMAS_DIR / f"{aci_class}.json").read_text())
            entity = next(iter(raw.values()))
            model = getattr(import_module(f"niwaki.models._generated.{pkg}.{aci_class}"), aci_class)

            expected_faults = dict(sorted((entity.get("faults") or {}).items()))
            assert model._fault_codes == expected_faults, f"{aci_class}: fault codes differ"

            raw_relation = entity.get("relationInfo") or {}
            expected_relation = (
                {
                    "cardinality": raw_relation.get("cardinality", ""),
                    "enforceable": bool(raw_relation.get("enforceable")),
                    "resolvable": bool(raw_relation.get("resolvable")),
                }
                if raw_relation
                else {}
            )
            assert model._relation_info == expected_relation, f"{aci_class}: relation differs"

            classes_with_faults += bool(expected_faults)
            total_codes += len(expected_faults)
            classes_with_relation += bool(expected_relation)

        # Coverage floors — a silent capture regression trips these.
        assert classes_with_faults >= 600, classes_with_faults
        assert total_codes >= 700, total_codes
        assert classes_with_relation >= 500, classes_with_relation
