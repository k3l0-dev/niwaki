"""Fabric access — 802.1X port-authentication policies (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/02_fabric-access/test_013_dot1x.py -m integration -s

The 802.1X shelf: the port-authentication policy across the admin-state x
host-mode cartesian, each carrying a configuration child that sweeps the
MAC-auth mode and the re-authentication boolean (plus timer variation). Values are
illustrative and cover the SDK surface, not a real access-control plan.

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

ADMIN_STATES = ("disabled", "enabled")
HOST_MODES = ("multi-auth", "multi-domain", "multi-host", "single-host")
MAC_AUTH = ("bypass", "eap")


def _common(obj: Cursor) -> None:
    """Attach the universal children (annotation, tag, RBAC domain marker)."""
    obj.mo(tagAnnotation, key="orchestrator", value="niwaki-it")
    obj.mo(tagTag, key="lifecycle", value="integration")
    obj.mo(aaaRbacAnnotation, domain="all")


def _name(admin: str, host: str) -> str:
    return f"niwaki-it-dot1x-{admin}-{host}"


def test_dot1x_port_authentication(live_aci: Niwaki) -> None:
    """802.1X port-authentication policy: admin x host-mode, with config child."""
    fab = infra()
    idx = 0
    for admin in ADMIN_STATES:
        for host in HOST_MODES:
            pol = fab.dot1x_port_authentication(
                _name(admin, host),
                administrative_state=admin,
                host_mode=host,
                description=f"802.1X admin/host-mode matrix - admin {admin}, {host}.",
            )
            _common(pol)
            re_auth = bool(idx & 1)
            cfg = pol.dot1x_port_authentication_config(
                mac_auth=MAC_AUTH[idx % len(MAC_AUTH)],
                re_authentication=re_auth,
                re_auth_period=3600 if re_auth else 7200,
                max_reauth_request=2,
                max_request=2,
                server_timeout=30,
                supplicant_timeout=30,
                tx_period=30,
            )
            _common(cfg)
            idx += 1
    fab.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for admin in ADMIN_STATES:
        for host in HOST_MODES:
            with contextlib.suppress(NotFoundError):
                aci.node(f"uni/infra/portauthpol-{_name(admin, host)}").delete()
