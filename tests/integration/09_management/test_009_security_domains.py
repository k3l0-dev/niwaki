"""Management — security-domain references (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/09_management/test_009_security_domains.py -m integration -s

A tenant classifies into a security domain through a domain reference
(``aaaDomainRef``) — the universal child that scopes an object for RBAC. The
controller forbids referencing the built-in ``mgmt`` / ``common`` / ``infra``
domains, so this file creates **user-defined** security domains (``aaaDomain``)
and binds tenant domain references to them. Coverage:

- user security domains with ``restricted_rbac_domain`` set both ways;
- one tenant per domain, plus a tenant that references several at once — so the
  single-reference and multi-reference shapes are both exercised.

The security domains live under ``uni/userext``; the tenants are dedicated
``niwaki-it-*`` tenants. One closed-world design, pushed once. Values are
illustrative.

``wipe(aci)`` (operator-only) removes only the named objects this file creates.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import Cursor, design
from niwaki.exceptions import NotFoundError
from niwaki.models.aaa.aaaDomainRef import aaaDomainRef
from niwaki.models.aaa.aaaRbacAnnotation import aaaRbacAnnotation
from niwaki.models.tag.tagAnnotation import tagAnnotation
from niwaki.models.tag.tagTag import tagTag

pytestmark = pytest.mark.integration

# User-created security domains — restricted_rbac_domain both ways.
SECDOMS = (
    ("niwaki-it-mgmt-secdom-open", "no"),
    ("niwaki-it-mgmt-secdom-restricted", "yes"),
    ("niwaki-it-mgmt-secdom-a", "no"),
    ("niwaki-it-mgmt-secdom-b", "yes"),
)
# Dedicated tenants; each references one or more of the domains above.
TENANTS = (
    ("niwaki-it-mgmt-secref-0", ("niwaki-it-mgmt-secdom-open",)),
    ("niwaki-it-mgmt-secref-1", ("niwaki-it-mgmt-secdom-restricted",)),
    ("niwaki-it-mgmt-secref-2", ("niwaki-it-mgmt-secdom-a", "niwaki-it-mgmt-secdom-b")),
)


def _common(obj: Cursor) -> None:
    """Attach the universal children present on almost every ACI class."""
    obj.mo(tagTag, key="niwaki-it", value="management")
    obj.mo(tagAnnotation, key="niwaki-it-note", value="exhaustive-provisioning")
    obj.mo(aaaRbacAnnotation, domain="all")


def test_security_domains(live_aci: Niwaki) -> None:
    root = design()

    aaa = root.aaa()
    for name, restricted in SECDOMS:
        dom = aaa.security_domain(
            name,
            restricted_rbac_domain=restricted,
            description=f"User security domain, restricted {restricted}.",
        )
        _common(dom)

    for tn_name, domains in TENANTS:
        ten = root.tenant(
            tn_name, description="Dedicated tenant classified by user security domains."
        )
        for dom_name in domains:
            ten.mo(aaaDomainRef, name=dom_name, description="Reference to a user security domain.")
        _common(ten)

    root.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    dns = [f"uni/tn-{tn_name}" for tn_name, _ in TENANTS]
    dns += [f"uni/userext/domain-{name}" for name, _ in SECDOMS]
    for dn in dns:
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
