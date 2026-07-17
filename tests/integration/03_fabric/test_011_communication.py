"""Fabric — management-access communication policies (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/03_fabric/test_011_communication.py -m integration -s

The management-access services are fixed singletons under a communication policy,
so combination coverage is spread across several named communication policies:
one per HTTP redirect state (with the other HTTP enums cycled), one per TLS
protocol-set combination for HTTPS (each carrying cipher entries in both states),
SSH policies covering different KEX / cipher / MAC ``Flags`` combinations, and a
final policy carrying the shell-in-a-box, setup, response-time and restart
services.

Exhaustive combination coverage, illustrative values — not a real fabric config.

``wipe(aci)`` (operator-only) removes every named communication policy.

# COVERAGE GAPS (curated parent, child/relation not reachable via the DSL):
#   - commHttps: key_ring (commRsKeyRing) and tp (commRsClientCertCA) are not
#     curated; commPol: telnet_service (commTelnet) is not curated.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import fabric
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

PREFIX = "niwaki-it-comm"
HTTP_REDIRECTS = ("disabled", "enabled", "tested")
# The APIC forbids combining TLSv1.3 with TLSv1 / TLSv1.1; the three combinations
# below together cover all four protocol values within that constraint.
TLS_COMBOS = (
    ("modern", "TLSv1.2,TLSv1.3"),
    ("legacy", "TLSv1,TLSv1.1,TLSv1.2"),
    ("single", "TLSv1.2"),
)
SSH_COMBOS = (
    ("single", "ecdh-sha2-nistp256", "aes256-ctr", "hmac-sha2-256"),
    (
        "multi",
        "ecdh-sha2-nistp256,ecdh-sha2-nistp384,curve25519-sha256",
        "aes128-ctr,aes192-ctr,aes256-ctr",
        "hmac-sha2-256,hmac-sha2-512",
    ),
)
CIPHERS = ("ECDHE-RSA-AES256-GCM-SHA384", "ECDHE-RSA-AES128-GCM-SHA256")


def _names() -> list[str]:
    names = [f"{PREFIX}-http-{r}" for r in HTTP_REDIRECTS]
    names += [f"{PREFIX}-tls-{slug}" for slug, _ in TLS_COMBOS]
    names += [f"{PREFIX}-ssh-{slug}" for slug, *_ in SSH_COMBOS]
    names.append(f"{PREFIX}-misc")
    return names


def test_http_services(live_aci: Niwaki) -> None:
    fab = fabric()
    for idx, redirect in enumerate(HTTP_REDIRECTS):
        comm = fab.communication_policy(
            f"{PREFIX}-http-{redirect}",
            description=f"Communication policy, HTTP redirect {redirect}.",
        )
        comm.http_service(
            description=f"HTTP service, redirect {redirect}.",
            admin_state="enabled",
            port=80,
            redirect_state=redirect,
            server_header_response="enabled" if idx % 2 == 0 else "disabled",
            cli_only_mode="disabled",
            node_exporter_service="enabled" if idx % 2 == 0 else "disabled",
            visore_access="enabled",
            access_control_allow_credential="enabled" if idx % 2 == 1 else "disabled",
        )
    fab.push(live_aci)


def test_https_services(live_aci: Niwaki) -> None:
    fab = fabric()
    for idx, (slug, protocols) in enumerate(TLS_COMBOS):
        comm = fab.communication_policy(
            f"{PREFIX}-tls-{slug}",
            description=f"Communication policy, TLS set {slug}.",
        )
        # Client-cert auth requires a CA trustpoint (commRsClientCertCA, not
        # curated — see COVERAGE GAPS), so it stays disabled.
        https = comm.http_ssl_configuration(
            description=f"HTTPS service, protocols {protocols}.",
            admin_state="enabled",
            port=443,
            ssl_protocols=protocols,
            client_cert_auth_state="disabled",
            server_header="enabled" if idx % 2 == 0 else "disabled",
        )
        for cipher_idx, cipher in enumerate(CIPHERS):
            https.ssl_cipher(
                cipher,
                cipher_state="enabled" if cipher_idx % 2 == 0 else "disabled",
            )
    fab.push(live_aci)


def test_ssh_services(live_aci: Niwaki) -> None:
    fab = fabric()
    for slug, kex, ciphers, macs in SSH_COMBOS:
        comm = fab.communication_policy(
            f"{PREFIX}-ssh-{slug}",
            description=f"Communication policy, SSH crypto set {slug}.",
        )
        comm.ssh_service(
            description=f"SSH service, {slug} crypto set.",
            admin_state="enabled",
            port=22,
            password_auth_state="enabled",
            kex_algorithms=kex,
            ssh_ciphers=ciphers,
            ssh_macs=macs,
        )
    fab.push(live_aci)


def test_other_services(live_aci: Niwaki) -> None:
    fab = fabric()
    comm = fab.communication_policy(
        f"{PREFIX}-misc",
        description="Communication policy carrying the remaining services.",
    )
    comm.shellinabox_service(
        description="Shell-in-a-box disabled (hardening).",
        admin_state="disabled",
    )
    comm.communication_setup(maximum_mos_in_query=100000)
    comm.response_time(
        admin_state="enabled",
        calc_window=300,
        top_n_requests=5,
        resp_time_threshold=85000,
    )
    comm.restart(toggle="disabled")
    fab.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for name in _names():
        with contextlib.suppress(NotFoundError):
            aci.node(f"uni/fabric/comm-{name}").delete()
