"""Fabric access — MACsec interface policies (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/02_fabric-access/test_011_macsec.py -m integration -s

The MACsec shelf, all under the fixed policy container: access-parameters
policies across the cipher-suite x confidentiality-offset x security-policy
cartesian; keychain policies carrying pre-shared keys; and the MACsec interface
policy (both admin states) binding a keychain. Values are illustrative and cover
the SDK surface, not a real MACsec deployment.

# COVERAGE GAPS (curated child reachable in the schema but not via a
# maker/bind/verb — reported, never forced):
#   - bind:macsecRsToParamPol@macsecIfPol — the interface policy's relation to its
#     access-parameters policy is not curated; only the keychain relation is.
# Managed-tag children (reachable only via .mo(), mark parent extMngdBy=msc,
# deliberately not configured): external_tag_instance / tag_instance.

This file owns only its niwaki-it-* policies; wipe(aci) removes them and is run by
hand (never by the suite).
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import Cursor, infra
from niwaki.exceptions import NotFoundError
from niwaki.models._generated.aaa.aaaRbacAnnotation import aaaRbacAnnotation
from niwaki.models._generated.tag.tagAnnotation import tagAnnotation
from niwaki.models._generated.tag.tagTag import tagTag

pytestmark = pytest.mark.integration

CIPHER_SLUG = {
    "gcm-aes-128": "128",
    "gcm-aes-256": "256",
    "gcm-aes-xpn-128": "xpn128",
    "gcm-aes-xpn-256": "xpn256",
}
OFFSET_SLUG = {"offset-0": "o0", "offset-30": "o30", "offset-50": "o50"}
SECPOL_SLUG = {"must-secure": "must", "should-secure": "should"}

ADMIN_STATES = ("enabled", "disabled")
KEYCHAINS = ("niwaki-it-macsec-kc-a", "niwaki-it-macsec-kc-b")


def _common(obj: Cursor) -> None:
    """Attach the universal children (annotation, tag, RBAC domain marker)."""
    obj.mo(tagAnnotation, key="orchestrator", value="niwaki-it")
    obj.mo(tagTag, key="lifecycle", value="integration")
    obj.mo(aaaRbacAnnotation, domain="all")


def _param_name(cipher: str, offset: str, secpol: str) -> str:
    return f"niwaki-it-mp-{CIPHER_SLUG[cipher]}-{OFFSET_SLUG[offset]}-{SECPOL_SLUG[secpol]}"


def _if_name(admin: str) -> str:
    return f"niwaki-it-macsec-if-{admin}"


def test_macsec_parameters(live_aci: Niwaki) -> None:
    """Access-parameters policies: cipher x confidentiality-offset x security-policy."""
    fab = infra()
    cont = fab.macsec()
    _common(cont)
    idx = 0
    for cipher in CIPHER_SLUG:
        for offset in OFFSET_SLUG:
            for secpol in SECPOL_SLUG:
                param = cont.parameters_policy(
                    _param_name(cipher, offset, secpol),
                    cipher_suite=cipher,
                    confidentiality_offset=offset,
                    security_policy=secpol,
                    key_server_priority=(16, 32, 200)[idx % 3],
                    replay_window=(0, 64, 148809600)[idx % 3],
                    sak_expiry_time="disabled" if idx % 2 else 300,
                    description=f"MACsec parameters matrix - {cipher}, {offset}, {secpol}.",
                )
                _common(param)
                idx += 1
    fab.push(live_aci)


def test_macsec_keychains_and_interface(live_aci: Niwaki) -> None:
    """Keychain policies with keys, and the interface policy binding one."""
    fab = infra()
    cont = fab.macsec()
    _common(cont)

    kc_a = cont.keychain_policy(
        KEYCHAINS[0],
        description="MACsec keychain - multiple pre-shared keys.",
    )
    _common(kc_a)
    kc_a.key_policy(
        "0a1b2c3d",
        pre_shared_key="0102030405060708090a0b0c0d0e0f10",
        start_time="now",
        end_time="infinite",
        description="MACsec key - primary.",
    )
    kc_a.key_policy(
        "0e5f6071",
        pre_shared_key="112233445566778899aabbccddeeff00",
        start_time="2027-01-01T00:00:00.000+00:00",
        end_time="infinite",
        description="MACsec key - secondary (start times in a keychain must be unique).",
    )

    kc_b = cont.keychain_policy(
        KEYCHAINS[1],
        description="MACsec keychain - single key.",
    )
    _common(kc_b)
    kc_b.key_policy(
        "0f0e0d0c",
        pre_shared_key="a1b2c3d4e5f60708090a0b0c0d0e0f10",
        start_time="now",
        end_time="infinite",
        description="MACsec key - single.",
    )

    for admin in ADMIN_STATES:
        macsec_if = fab.macsec_interface_policy(
            _if_name(admin),
            admin_state=admin,
            description=f"MACsec interface admin sweep - {admin}, binds keychain.",
        )
        _common(macsec_if)
        macsec_if.bind(keychain=KEYCHAINS[0])

    fab.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    param_dns = [
        f"uni/infra/macsecpcont/paramp-{_param_name(c, o, s)}"
        for c in CIPHER_SLUG
        for o in OFFSET_SLUG
        for s in SECPOL_SLUG
    ]
    kc_dns = [f"uni/infra/macsecpcont/keychainp-{kc}" for kc in KEYCHAINS]
    if_dns = [f"uni/infra/macsecifp-{_if_name(a)}" for a in ADMIN_STATES]
    for dn in (*param_dns, *kc_dns, *if_dns):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
