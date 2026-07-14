"""Golden wire format — the contract world.

Offline counterpart of the live contract act: ``vzAny`` (the VRF-wide EPG
collection, with its own Rs classes), directional filters on a subject
(``vzInTerm``/``vzOutTerm``), contract exceptions, the out-of-band contract,
and the six contract labels wherever the MIT hangs them (EPG, ESG, vzAny,
subject).

The assertion is the flattened ``{DN: attributes}`` view the push engine
sends — it pins the RN formats (``any``, ``intmnl``, ``provlbl-<name>``,
``excp-<name>`` …) and the wire property names.
"""

from __future__ import annotations

from niwaki.design import Cursor, tenant
from niwaki.design._compiler import compile_ops
from niwaki.design._resolver import resolve


def contract_world() -> Cursor:
    """One tenant exercising every curated position of the contract world."""
    cfg = tenant("T")
    cfg.filter("f").entry("e", tcp=8080)

    # A contract, its subject with both-direction filter, and its labels.
    contract = cfg.contract("web")
    contract.subject("both-ways", provider_label_match_type="AtleastOne").bind(filter="f")
    # An exception lives on the subject AND on the contract: the nearest level
    # wins, so a contract-level exception is declared from the contract cursor.
    contract.exception("skip-dev", field="Tenant", cons_regex="dev-.*")

    # A subject that stops applying both ways: one filter per direction.
    directional = contract.subject("directional")
    directional.in_term(qos_class_id="level2").bind(filter="f")
    directional.out_term(qos_class_id="level3").bind(filter="f")
    directional.provider_subject_label("gold", tag="green")
    directional.consumer_subject_label("silver", tag="blue", complement=True)

    cfg.imported_contract("imp").bind(contract="web")

    # Out-of-band contract (management), with its own subject and exception.
    oob = cfg.oob_contract("oob-mgmt", scope="context")
    oob.subject("oob-s").bind(filter="f")
    oob.exception("oob-skip", field="EPg", prov_regex="mgmt-.*")

    # vzAny — contracts for the whole VRF, through Rs classes of its own.
    vzany = cfg.vrf("v").vzany(match_type="AtleastOne", preferred_group_member="enabled")
    vzany.provide("web").consume("web").bind(imported_contract="imp")
    vzany.provider_label("vrf-gold", tag="gold")
    vzany.consumer_contract_label("vrf-cc", tag="cyan")

    # The same label vocabulary on an EPG and on an ESG.
    app = cfg.app("ap")
    app.epg("web").provider_label("gold", tag="green", complement=False)
    app.esg("sec").bind(vrf="v").consumer_label("esg-silver", tag="silver")
    return cfg


def _flatten(cursor: Cursor) -> dict[str, dict[str, str]]:
    """The DN → attributes map the push engine writes, minus the ``dn`` echo."""
    root = cursor.design_node.root()
    flat = {}
    for op in compile_ops(root, resolve(root)):
        assert op.payload is not None
        ((_, body),) = op.payload.items()
        flat[op.dn] = {k: v for k, v in body["attributes"].items() if k != "dn"}
    return flat


class TestContractWorldGolden:
    def test_subject_directions_and_labels(self) -> None:
        flat = _flatten(contract_world())
        subj = "uni/tn-T/brc-web/subj-directional"
        assert {dn: a for dn, a in flat.items() if dn.startswith(subj)} == {
            subj: {"name": "directional"},
            # Terminals are singletons; each carries its own filter binding.
            f"{subj}/intmnl": {"prio": "level2"},
            f"{subj}/intmnl/rsfiltAtt-f": {"tnVzFilterName": "f"},
            f"{subj}/outtmnl": {"prio": "level3"},
            f"{subj}/outtmnl/rsfiltAtt-f": {"tnVzFilterName": "f"},
            f"{subj}/provsubjlbl-gold": {"name": "gold", "tag": "green"},
            f"{subj}/conssubjlbl-silver": {
                "name": "silver",
                "tag": "blue",
                "isComplement": "true",
            },
        }

    def test_exception_attaches_to_the_level_it_is_declared_on(self) -> None:
        """``exception`` exists on the contract and on the subject alike."""
        flat = _flatten(contract_world())
        assert flat["uni/tn-T/brc-web/excp-skip-dev"] == {
            "name": "skip-dev",
            "field": "Tenant",
            "consRegex": "dev-.*",
        }
        assert flat["uni/tn-T/oobbrc-oob-mgmt/excp-oob-skip"] == {
            "name": "oob-skip",
            "field": "EPg",
            "provRegex": "mgmt-.*",
        }

    def test_vzany_uses_its_own_relation_classes(self) -> None:
        """Not ``fvRsProv``/``fvRsCons``: vzAny has ``vzRsAnyTo*``."""
        flat = _flatten(contract_world())
        any_dn = "uni/tn-T/ctx-v/any"
        assert {dn: a for dn, a in flat.items() if dn.startswith(any_dn)} == {
            any_dn: {"matchT": "AtleastOne", "prefGrMemb": "enabled"},
            f"{any_dn}/rsanyToProv-web": {"tnVzBrCPName": "web"},
            f"{any_dn}/rsanyToCons-web": {"tnVzBrCPName": "web"},
            f"{any_dn}/rsanyToConsIf-imp": {"tnVzCPIfName": "imp"},
            f"{any_dn}/provlbl-vrf-gold": {"name": "vrf-gold", "tag": "gold"},
            f"{any_dn}/cCtrctLbl-vrf-cc": {"name": "vrf-cc", "tag": "cyan"},
        }

    def test_oob_contract_carries_a_subject(self) -> None:
        flat = _flatten(contract_world())
        assert flat["uni/tn-T/oobbrc-oob-mgmt"] == {"name": "oob-mgmt", "scope": "context"}
        assert flat["uni/tn-T/oobbrc-oob-mgmt/subj-oob-s/rssubjFiltAtt-f"] == {
            "tnVzFilterName": "f"
        }

    def test_labels_speak_the_same_word_everywhere(self) -> None:
        """One vocabulary — EPG, ESG, vzAny and subject all take the labels."""
        flat = _flatten(contract_world())
        assert flat["uni/tn-T/ap-ap/epg-web/provlbl-gold"] == {
            "name": "gold",
            "tag": "green",
            "isComplement": "false",
        }
        assert flat["uni/tn-T/ap-ap/esg-sec/conslbl-esg-silver"] == {
            "name": "esg-silver",
            "tag": "silver",
        }
