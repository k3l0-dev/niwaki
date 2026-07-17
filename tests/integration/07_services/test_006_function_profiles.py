"""L4-L7 services — function profiles and instance config model (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/07_services/test_006_function_profiles.py -m integration -s

The operator builds the function-profile model (container -> groups -> profiles ->
device / function / group configs) and the instantiated config model: an L4-L7 policy
container with folder instances, and tenant-level folder instances swept across every
``scoped_by`` value and every ``cardinality`` value, with the ``locked`` / ``mandatory``
booleans on their param instances and relations.

Combination sweep — not a production catalogue. ``wipe(aci)`` (operator-only) drops the
dedicated tenant.

Universal children (``tagInst`` / ``tagExtMngdInst``) carry no typed maker in the DSL.

# COVERAGE GAPS (curated children with no reachable maker/bind — reported, not forced):
#   - folder / param / relation / instance content under a function-profile config
#     (vnsAbsFolder / vnsAbsParam / vnsAbsCfgRel / vnsFolderInst / vnsParamInst /
#     vnsCfgRelInst @vnsAbsDevCfg|vnsAbsFuncCfg|vnsAbsGrpCfg) has no maker
#   - bind:vnsRsProfToMFunc / vnsRsProfToCloudModeMDev @vnsAbsFuncProf
#   - maker:vnsFolderInst@vnsFolderInst (one level only) plus bind:vnsRsFolderInstToMFolder
#     / vnsRsCfgToConn / vnsRsScopeToTerm @vnsFolderInst
"""

from __future__ import annotations

import contextlib
import itertools

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TN = "niwaki-it-fprof"

CARDINALITIES = ("1", "n", "unspecified")
SCOPED_BY = ("ap", "bd", "epg", "none", "tenant")

POLICY_CONTRACT = "niwaki-it-ct"
POLICY_GRAPH = "niwaki-it-graph"
POLICY_NODE = "FW1"


def test_function_profile_model(live_aci: Niwaki) -> None:
    tn = tenant(TN, description="Function profile and instance config-model sweep.")

    container = tn.function_profile_container(description="L4-L7 function-profile container.")
    # Two groups, each with two profiles, each with all three config kinds.
    for group_index in range(2):
        group = container.function_profile_group(
            f"niwaki-it-grp{group_index}", description=f"Profile group {group_index}."
        )
        for profile_index in range(2):
            profile = group.function_profile(
                f"niwaki-it-prof{group_index}{profile_index}",
                description=f"Profile {group_index}.{profile_index}.",
                src_mode="local",
            )
            profile.device_config(name="device", description="Device-shared config.")
            profile.function_config(name="function", description="Function config.")
            profile.group_config(name="group", description="Group-shared config.")

    tn.push(live_aci)


def test_instance_config_model(live_aci: Niwaki) -> None:
    tn = tenant(TN, description="Function profile and instance config-model sweep.")

    # L4-L7 policy container with folder instances (cardinality sweep).
    container = tn.policy_container(POLICY_CONTRACT, POLICY_GRAPH, POLICY_NODE)
    for index, cardinality in enumerate(CARDINALITIES):
        folder = container.folder_instance(
            POLICY_CONTRACT,
            POLICY_GRAPH,
            POLICY_NODE,
            f"pol-folder-{index}",
            cardinality=cardinality,
            dev_ctx_lbl="fw",
            meta_folder_key="ExternalIf",
            locked=bool(index % 2),
            scoped_by="tenant",
        )
        folder.param_instance(
            "name",
            cardinality=cardinality,
            meta_param_key="name",
            locked=bool(index % 2),
            mandatory=not bool(index % 2),
            validation="string",
            value="outside",
        )
        folder.relation(
            "peer",
            cardinality=cardinality,
            key="rel",
            locked=not bool(index % 2),
            mandatory=bool(index % 2),
            target_name="inside",
        )

    # Tenant-level folder instances — sweep every scoped_by value.
    card_cycle = itertools.cycle(CARDINALITIES)
    locked_cycle = itertools.cycle((True, False))
    for index, scope in enumerate(SCOPED_BY):
        folder = tn.folder_instance(
            POLICY_CONTRACT,
            POLICY_GRAPH,
            POLICY_NODE,
            f"tn-folder-{scope}",
            cardinality=next(card_cycle),
            dev_ctx_lbl="fw",
            meta_folder_key="Global",
            locked=next(locked_cycle),
            scoped_by=scope,
        )
        folder.param_instance(
            "log-level",
            cardinality=next(card_cycle),
            meta_param_key="logLevel",
            locked=next(locked_cycle),
            mandatory=bool(index % 2),
            validation="enum",
            value="informational",
        )
        folder.relation(
            "monitor",
            cardinality=next(card_cycle),
            key="mon",
            locked=next(locked_cycle),
            mandatory=not bool(index % 2),
            target_name="default",
        )

    tn.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (f"uni/tn-{TN}",):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
