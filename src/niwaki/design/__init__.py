"""Niwaki design DSL — declarative, IDE-friendly ACI provisioning.

Build a detached design tree with operator vocabulary (no APIC class names,
no session), then validate and push it in one call.  The DSL covers the whole
``uni`` subtree — tenants, access policies (``infra``), fabric policies
(``fabric``), and controller policies — through one uniform vocabulary::

    from niwaki import Niwaki
    from niwaki.design import tenant

    config = (
        tenant("prod")
        .app("prod")
            .epg("frontend").bind(bd="frontend")
        .bd("frontend")
            .set(unicast_routing=True)
            .bind(vrf="prod")
            .subnet("10.0.1.1/24")
        .vrf("prod")
    )

    with Niwaki("https://apic.example.com", "admin", "secret") as aci:
        config.push(aci, mode="strict")   # one atomic POST, all-or-nothing

Every design is rooted at ``polUni``; ``design()`` starts an empty
multi-domain design, while ``tenant()`` / ``infra()`` / ``fabric()`` /
``controller()`` are shorthands that declare the first domain and return its
cursor — sibling domains stay one maker call away::

    cfg = design()
    cfg.fabric().datetime_policy("prod-ntp")
    cfg.infra().vlan_pool("prod", "static").range("vlan-100", "vlan-199")
    cfg.tenant("prod").vrf("main")

Core rules (see the *Design-first architecture* page in the documentation):

- **Structure is literal**: every maker maps 1:1 to a real APIC child class;
  nothing is silently created or hidden.
- **Verbatim is translated**: names and parameters use operator vocabulary
  (``entry("http", tcp=80)``, ``scope="vrf"``).
- **References are lazy**: ``bind()`` / ``provide()`` / ``consume()`` resolve
  at push time — forward references allowed, closed-world validated.
- **No I/O during construction**: transport is injected at ``push()`` only.
"""

from __future__ import annotations

from niwaki.design._compose import merge
from niwaki.design._cursor import Cursor
from niwaki.design._emit import to_code
from niwaki.design._generated_cursors import aaa, controller, design, fabric, infra, tenant
from niwaki.design._import import ImportProblem, from_payload, to_design
from niwaki.design._node import Ref, ref
from niwaki.design._push import PlanResult, PushReport
from niwaki.design._reconcile import Reconciliation, reconcile
from niwaki.design._verify import ExternalRef, RefCheck
from niwaki.design._view import DesignView, DesignViewBind, DesignViewNode

__all__ = [
    "Cursor",
    "DesignView",
    "DesignViewBind",
    "DesignViewNode",
    "ExternalRef",
    "ImportProblem",
    "PlanResult",
    "PushReport",
    "Reconciliation",
    "Ref",
    "RefCheck",
    "aaa",
    "controller",
    "design",
    "fabric",
    "from_payload",
    "infra",
    "merge",
    "reconcile",
    "ref",
    "tenant",
    "to_code",
    "to_design",
]
