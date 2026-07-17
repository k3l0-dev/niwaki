"""Fabric — MACsec parameters, keychains and interface policies (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/03_fabric/test_003_macsec.py -m integration -s

The fabric MACsec policy container (a fabric singleton) holds the parameter and
keychain policies; the fabric MACsec interface policies live directly under
``uni/fabric``. This file provisions one parameter policy per
``(cipher-suite, security-policy)`` pair (full cartesian of the four cipher
suites and both security policies), keychains each carrying several keys, and one
interface policy per ``(admin-state, auto-keys)`` combination.

Exhaustive combination coverage, illustrative values — not a real fabric config.

``wipe(aci)`` (operator-only) removes the parameter/keychain policies (under the
singleton container) and the named interface policies.

# COVERAGE GAPS (curated parent, child/relation not reachable via the DSL):
#   - macsecFabIfPol: source_to_ifpol_relation (macsecRsToParamPol) and
#     source_to_keychain_relation (macsecRsToKeyChainPol) — the interface policy
#     cannot be wired to its parameters/keychain policy.
"""

from __future__ import annotations

import contextlib
import itertools

import pytest

from niwaki import Niwaki
from niwaki.design import fabric
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

PARAM = "niwaki-it-macsec-param"
KEYCHAIN = "niwaki-it-macsec-kc"
IFPOL = "niwaki-it-macsec-if"

CIPHER_SUITES = ("gcm-aes-128", "gcm-aes-256", "gcm-aes-xpn-128", "gcm-aes-xpn-256")
SECURITY_POLICIES = ("must-secure", "should-secure")
IF_ADMIN = ("enabled", "disabled")
IF_AUTOKEYS = (True, False)


def test_macsec_parameter_policies(live_aci: Niwaki) -> None:
    fab = fabric()
    container = fab.macsec_fabric()
    for cipher, secpol in itertools.product(CIPHER_SUITES, SECURITY_POLICIES):
        container.parameters_policy(
            f"{PARAM}-{cipher}-{secpol}",
            description=f"MACsec parameters: {cipher}, {secpol}.",
            cipher_suite=cipher,
            security_policy=secpol,
            replay_window=64 if secpol == "must-secure" else 0,
            sak_expiry_time=60 if cipher.endswith("256") else "disabled",
        )
    fab.push(live_aci)


def test_macsec_keychains(live_aci: Niwaki) -> None:
    fab = fabric()
    container = fab.macsec_fabric()
    # Two keychains, each with several hex-named keys (immediate + scheduled).
    for chain_idx in range(2):
        keychain = container.keychain_policy(
            f"{KEYCHAIN}-{chain_idx}",
            description=f"MACsec keychain {chain_idx}.",
        )
        for key_idx in range(3):
            # Start times within a keychain must be unique, so stagger by month.
            keychain.key_policy(
                f"{chain_idx:02x}{key_idx:02x}",
                description=f"MACsec key {key_idx} of keychain {chain_idx}.",
                pre_shared_key="0123456789abcdef0123456789abcdef",
                start_time=f"2030-0{key_idx + 1}-01T00:00:00.000+00:00",
                end_time="infinite",
            )
    fab.push(live_aci)


def test_macsec_interface_policies(live_aci: Niwaki) -> None:
    fab = fabric()
    for admin, autokeys in itertools.product(IF_ADMIN, IF_AUTOKEYS):
        fab.macsec_fabric_interface_policy(
            f"{IFPOL}-{admin}-{'auto' if autokeys else 'manual'}",
            description=f"MACsec interface policy: admin {admin}, auto-keys {autokeys}.",
            admin_state=admin,
            use_system_generated_keys=autokeys,
        )
    fab.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it.

    The MACsec container singleton (uni/fabric/macsecpcontfab) is left in place;
    only the parameter, keychain and interface policies are removed.
    """
    dns: list[str] = []
    for cipher, secpol in itertools.product(CIPHER_SUITES, SECURITY_POLICIES):
        dns.append(f"uni/fabric/macsecpcontfab/fabparamp-{PARAM}-{cipher}-{secpol}")
    dns += [f"uni/fabric/macsecpcontfab/keychainp-{KEYCHAIN}-{i}" for i in range(2)]
    for admin, autokeys in itertools.product(IF_ADMIN, IF_AUTOKEYS):
        dns.append(f"uni/fabric/macsecfabifp-{IFPOL}-{admin}-{'auto' if autokeys else 'manual'}")
    for dn in dns:
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
