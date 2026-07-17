"""Fabric — date/time (NTP) policies and display format (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/03_fabric/test_009_datetime.py -m integration -s

One date/time policy per representative
``(admin, server-mode, master-mode, authentication)`` combination, each carrying
authentication keys (both key types, trusted both ways) and NTP providers (the
preferred and true-chimer flags toggled, the provider referencing an
authentication key and associated with the out-of-band management EPG). The
datetime display format is a fabric singleton, configured once.

Exhaustive combination coverage, illustrative values — not a real fabric config.

``wipe(aci)`` (operator-only) removes every named date/time policy; the display
format singleton is left as-is.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import fabric
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

OOB_MGMT_EPG = "uni/tn-mgmt/mgmtp-default/oob-default"

PREFIX = "niwaki-it-ntp"
# (slug, admin, server, master, auth)
POLICIES = (
    ("basic", "enabled", "disabled", "disabled", "disabled"),
    ("server", "enabled", "enabled", "disabled", "disabled"),
    ("master", "enabled", "enabled", "enabled", "disabled"),
    ("auth", "enabled", "disabled", "disabled", "enabled"),
    ("off", "disabled", "disabled", "disabled", "disabled"),
)


def test_datetime_policies(live_aci: Niwaki) -> None:
    fab = fabric()
    for slug, admin, server, master, auth in POLICIES:
        policy = fab.datetime_policy(
            f"{PREFIX}-{slug}",
            description=f"NTP policy {slug}: admin {admin}, server {server}, auth {auth}.",
            admin_state=admin,
            server_mode=server,
            master_clock_mode_toggle=master,
            authentication_state=auth,
            stratum_value=8,
        )
        # Two authentication keys: MD5/trusted and SHA1/untrusted.
        policy.ntp_auth_key(
            1,
            description="MD5 NTP authentication key, trusted.",
            key="0123456789abcdef",
            type_of_authentication_key="md5",
            trusted_state=True,
        )
        policy.ntp_auth_key(
            2,
            description="SHA1 NTP authentication key, untrusted.",
            key="fedcba9876543210",
            type_of_authentication_key="sha1",
            trusted_state=False,
        )
        # Two providers: preferred + true-chimer, and neither.
        preferred = policy.ntp_provider(
            f"1.{slug}.pool.niwaki.example",
            description="Preferred NTP provider, true-chimer, keyed.",
            key_id=1,
            preferred_state=True,
            min_poll=4,
            max_poll=6,
            truechimer_status="enabled",
        )
        preferred.authentication_key(1)
        preferred.bind_dn(management_epg=OOB_MGMT_EPG)
        secondary = policy.ntp_provider(
            f"2.{slug}.pool.niwaki.example",
            description="Secondary NTP provider, not preferred.",
            key_id=2,
            preferred_state=False,
            min_poll=6,
            max_poll=10,
            truechimer_status="disabled",
        )
        secondary.authentication_key(2)
        secondary.bind_dn(management_epg=OOB_MGMT_EPG)
    fab.push(live_aci)


# The datetime display format is a fabric singleton, so its mutually-exclusive
# settings are factored across successive pushes: both display formats, both
# offset states and a spread of time zones across the globe.
FORMAT_SPECS = (
    ("utc", "enabled", "p60_Europe-London"),
    ("local", "disabled", "n480_America-Anchorage"),
    ("utc", "disabled", "p330_Asia-Kolkata"),
    ("local", "enabled", "p600_Australia-Brisbane"),
    ("utc", "enabled", "n240_America-New_York"),
    ("local", "disabled", "p540_Asia-Tokyo"),
)


def test_datetime_format(live_aci: Niwaki) -> None:
    for display_format, offset, time_zone in FORMAT_SPECS:
        fabric().datetime_format(
            description=f"Datetime display format: {display_format}, offset {offset}, {time_zone}.",
            display_format=display_format,
            offset=offset,
            time_zone=time_zone,
        ).push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it.

    The datetime format singleton is left as-is; only the named NTP policies are
    removed.
    """
    for slug, *_ in POLICIES:
        with contextlib.suppress(NotFoundError):
            aci.node(f"uni/fabric/time-{PREFIX}-{slug}").delete()
