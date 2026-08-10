"""The curated secret-redaction policy — what a snapshot must never publish.

The mechanical layer is the schema's ``secure`` flag: the APIC omits those
properties from every read, so they cannot leak through a read path.  Measured
on the 6.0(9c) catalogue, that flag is **broad** — BGP MD5 passwords, AAA
monitoring passwords, KMS private keys, NTP client passwords, MACsec and
IPsec pre-shared keys, remote-path passwords, firmware-source passwords, the
PKI export passphrase and every private key are all flagged.  The mechanical
layer carries the overwhelming majority.

**But the flag is not a policy.**  A bounded set of real secret material is
left unflagged — the "flag forgotten on one copy" pattern — and some of it
echoes back in cleartext on a read (measured: ``vnsCCred.value``).  These are
curated explicitly:

- :data:`SECRET_VALUE_POSITIONS` — properties whose *value* is secret despite
  the missing flag (the SNMP trap community ``snmpTrapDest.secName``, the HSRP
  and BFD-multihop authentication keys whose secure siblings ARE flagged, the
  local-user OTP seed, device credentials, firmware-source auth passwords):
  redact the value.
- :data:`SECRET_DN_POSITIONS` — *naming* properties whose value is secret, so
  the secret is in the object's own DN and every descendant's: the SNMP
  community profile, a login session named by its token hash, a JWT site named
  by its API key.  No value redaction reaches these; a consumer warns or drops
  the subtree.

Every other candidate the sweep raises is dismissed **with a reason**
(:data:`DISMISS_RULES` for collision families, :data:`REVIEWED_NOT_SECRET`
for individual positions).  The drift guard (``tests/test_secrets.py``)
recomputes three sweep nets against the shipped catalogue — property-name
patterns, class-name patterns, and the **secure-sibling asymmetry** (a
configurable non-secure value-shaped property on a class that also has a
flagged one, the single most reliable signal for a forgotten flag) — and fails
on any hit that is neither ``secure``-flagged, curated, nor dismissed.  A
future train's new secret-shaped property breaks the build until a human
triages it.
"""

from __future__ import annotations

import re
import sqlite3
from functools import cache
from pathlib import Path

# ── The sweep definition (shared with the drift guard) ────────────────────────
# Three nets. A: property names that smell of secret material. B: classes whose
# name announces credential content, catching value props named plainly
# (name/value/key/secName). C: the secure-sibling asymmetry — a value-shaped
# configurable non-secure prop on a class that also carries a secure one.

PROP_PATTERN = re.compile(
    r"(passw|pwd|passphrase|community|secret|token|credential|privkey|private.?key"
    r"|authkey|shared.?key|presharedkey|psk\b|key0|keyring|secname|otpkey|userkey"
    r"|accprovision|authpass)",
    re.IGNORECASE,
)
CLASS_PATTERN = re.compile(
    r"(Cred|Community|Passw|Pwd|Secret|Token|AuthKey|PresharedKey|Psk|Auth)",
    re.IGNORECASE,
)
#: Props inspected on class-pattern hits (beside every naming prop).
VALUE_PROPS = frozenset(
    {"name", "value", "key", "community", "token", "pwd", "password", "passphrase", "secName"}
)
#: Value-shaped names for the asymmetry net (arm C) — deliberately narrow, so a
#: secure-sibling class does not drag its every ``descr``/``timeout`` into the
#: sweep.  ``keyId``/``keyType``/``keyName``/``ownerKey`` are excluded by
#: :data:`_ASYM_EXCLUDE` (indexes and references, not material).
_ASYM_VALUE = re.compile(
    r"(passw|pwd|secret|token|credential|privkey|private.?key|authkey|presharedkey"
    r"|psk|otpkey|userkey|authpass|\bkey\b|apikey|sharedkey)",
    re.IGNORECASE,
)
_ASYM_EXCLUDE = re.compile(r"(keyid|keytype|keyname|ownerkey|keyringdn)$", re.IGNORECASE)


# ── Curated: secret VALUES the schema does not flag ───────────────────────────
# (class wire name, property wire name) — redact the value on sight.

SECRET_VALUE_POSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        # Device credential in the clear; vnsCCredSecret.value carries the
        # secure flag for the same material — the flag was forgotten here.
        # MEASURED live (6.0(9c), 2026-08-10): the value echoes back on a read.
        ("vnsCCred", "value"),
        # The SNMP trap destination's v1/v2c security name IS the community
        # string, in the clear (shown in the GUI, echoes on read).
        ("snmpTrapDest", "secName"),
        # HSRP simple-authentication key. secureAuthKey on the same class is
        # flagged; this plain copy is not.
        ("hsrpGroupPol", "key"),
        ("hsrpAGroupPol", "key"),
        # BFD multihop authentication key — bfdAuthP.key (single-hop) is
        # flagged secure, its multihop sibling class is not.
        ("bfdMhAuthP", "key"),
        # The PIM authentication key family.  OSPF's authKey is flagged
        # secure; PIM's concrete, rendered copy, and abstract parent are not.
        ("pimIfPol", "authKey"),
        ("pimIfDef", "authKey"),
        ("rtdmcAIfPol", "authKey"),
        # The local user's OTP/TOTP seed — possession forges valid codes.
        # aaaUser.pwd on the same class IS flagged; the seed is not.
        ("aaaUser", "otpkey"),
        # Kubernetes/CNI cluster provisioning material on the injected details.
        ("vmmInjectedClusterDetails", "userKey"),
        ("vmmInjectedClusterDetails", "accProvisionInput"),
        # Firmware/package download source authentication passwords (the
        # `.password` sibling IS flagged secure; `.authPass` is not).
        ("firmwareSource", "authPass"),
        ("firmwareCcoSource", "authPass"),
        ("firmwareInternalSource", "authPass"),
        ("firmwareOSource", "authPass"),
        ("vnsSvcPkgSource", "authPass"),
        ("vnsVmmConfigFile", "authPass"),
        # Certificate-request challenge password.
        ("pkiCertReq", "pwd"),
        # Cleartext credentials on the password/role change action objects.
        ("aaaChangePassword", "newPassword"),
        ("aaaChangePassword", "oldPassword"),
        ("aaaChangeRole", "pwd"),
        # Node bootstrap token on the certificate-response action object.
        ("aaaNodeCertResp", "token"),
        # Smart-licensing registration token — possession is authority.
        ("licenseLicPolicy", "regTokenId"),
        # An API key, sitting beside jwtPrivateKey (flagged) on the same class.
        ("pkiWebTokenData", "jwtApiKey"),
        # Abstract parent of snmpCommunityP: the same community string, held
        # as a plain property where the concrete holds it as naming.
        ("snmpACommunityP", "name"),
    }
)

# ── Curated: secret NAMING props — the DN itself is contaminated ──────────────
# No value redaction can help: the secret is in the object's RN, and in the DN
# of every descendant.  A consumer must warn, or drop the subtree.

SECRET_DN_POSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        # The SNMP community string IS the object's name: community-{name}.
        ("snmpCommunityP", "name"),
        # Login session objects are named by their token hash.
        ("aaaActiveUserSession", "hashToken"),
        ("aaaDeletedUserSession", "hashToken"),
        # A JWT site record is named by its API key: sitejwtpubkey-{jwtApiKey}.
        ("pkiSiteJwtPubKey", "jwtApiKey"),
    }
)

# ── Dismissal rules — whole pattern-collision families, one reason each ───────

DISMISS_RULES: tuple[tuple[re.Pattern[str], re.Pattern[str], str], ...] = (
    (
        re.compile(r"^(fcReceive|fcTransmit)B2BCredit"),
        re.compile(r".*"),
        "Fibre Channel buffer-to-buffer flow-control credits — 'credit', not credentials",
    ),
    (
        re.compile(r"(svcredir|SvcRedir)"),
        re.compile(r".*"),
        "service-redirect (PBR) machinery — 'redir' collides with the Cred pattern",
    ),
    (
        re.compile(r".*"),
        re.compile(r"^(community|acommunity)$"),
        "BGP community attributes — route tags (65000:100), not credentials",
    ),
    (
        re.compile(r".*"),
        re.compile(r".*(Dn|DN)$"),
        "a DN pointer to another object, never key material itself",
    ),
    (
        re.compile(r".*"),
        re.compile(r"^authKeyId$"),
        "an authentication key INDEX (1-255), not the key itself",
    ),
    (
        re.compile(r"auth", re.IGNORECASE),
        re.compile(r"^(name|id|tDn)$"),
        "an authentication policy/scaffolding object's label or reference, "
        "not key material (the key, where one exists, is curated separately)",
    ),
    (
        re.compile(r".*"),
        re.compile(r"^tn.*AuthKeyId$"),
        "a reference to an NTP authentication key by its id, not the key",
    ),
    (
        re.compile(r".*"),
        re.compile(
            r"^(monitoringPassword|password|pwd|passphrase|preSharedKey"
            r"|userPasswd|clientPassword|snKmsPrivKey|rsaPrivateKey"
            r"|identityPrivateKeyContents|identityPrivateKeyPassphrase|privKey"
            r"|authKey|key)$"
        ),
        "SECURE-FLAGGED on its class — the APIC omits it from reads (mechanical layer); "
        "only the unflagged copies are curated above",
    ),
)

# ── Explicit dismissals — every remaining sweep hit, with its reason ──────────

REVIEWED_NOT_SECRET: dict[tuple[str, str], str] = {
    # AAA password policy knobs, carrying no password.
    ("aaaPwdStrengthProfile", "pwdClassFlags"): "password policy knob",
    ("aaaPwdStrengthProfile", "pwdMaxLength"): "password policy knob",
    ("aaaPwdStrengthProfile", "pwdMinLength"): "password policy knob",
    ("aaaPwdStrengthProfile", "pwdStrengthTestType"): "password policy knob",
    ("aaaPwdStrengthProfile", "name"): "policy object's label",
    ("aaaPwdProfile", "name"): "policy object's label",
    ("aaaUser", "clearPwdHistory"): "password policy knob",
    ("aaaUser", "pwdLifeTime"): "password policy knob",
    ("aaaUser", "pwdUpdateRequired"): "password policy knob",
    ("aaaUserEp", "pwdStrengthCheck"): "password policy knob",
    # References naming another object, not the material.
    ("commRsKeyRing", "tnPkiKeyRingName"): "keyring reference by name",
    ("isakmpRsProfileToKeyring", "tnIsakmpKeyringName"): "keyring reference by name",
    ("datetimeNtpAuth", "id"): "NTP key id (naming) — an integer index",
    ("datetimeNtpAuthKey", "id"): "NTP key id (naming) — an integer index",
    # Status booleans, endpoints, CORS knobs.
    ("bgpAPeerDef", "passwdSet"): "boolean 'a password is set'",
    ("bgpInfraPeerDef", "passwdSet"): "boolean 'a password is set'",
    ("bgpPeerDef", "passwdSet"): "boolean 'a password is set'",
    ("commSsh", "passwordAuth"): "enable/disable knob",
    ("commHttp", "accessControlAllowCredential"): "CORS allow-credentials knob",
    ("commHttps", "accessControlAllowCredential"): "CORS allow-credentials knob",
    ("commWeb", "accessControlAllowCredential"): "CORS allow-credentials knob",
    ("aaaOauthProvider", "tokenEndpoint"): "OAuth endpoint URL",
    # Knobs on otherwise secret-bearing classes (their material IS flagged).
    ("pkiExportEncryptionKey", "passphraseKeyDerivationVersion"): "algorithm version knob",
    ("pkiWebTokenData", "webtokenTimeoutSeconds"): "timeout knob",
    ("pkiWebTokenData", "name"): "object's label; key material is flagged secure",
    ("pkiSchedulerToken", "name"): "object's label; key material is flagged secure",
    ("fabricSecurityToken", "name"): "object's label; token is flagged secure; fixed RN 'sectok'",
    ("fabricSecurityTokenHelper", "name"): "object's label",
    ("topoctrlSecurityToken", "name"): "object's label",
    ("cloudsecPreSharedKey", "name"): "object's label; pskString is flagged secure",
    ("cloudsecPreSharedKey", "index"): "PSK ordering index",
    ("datetimeANtpAuthKey", "name"): "object's label; key is flagged secure",
    ("datetimeNtpAuthKey", "name"): "object's label; key is flagged secure",
    ("fvPasswordConfig", "name"): "object's label; password is flagged secure",
    ("fvPasswordConfigDef", "name"): "object's label",
    ("cloudCredentials", "name"): "the credential set's label; key material is flagged secure",
    # Usernames and identifiers: identifying, not authenticating.
    ("vnsCCred", "name"): "the credential's username/label — identifier, not the value",
    ("vnsCCredSecret", "name"): "the credential's username/label — identifier, not the value",
    ("vnsMCred", "name"): "device-package credential meta-model name",
    ("vnsMCredSecret", "name"): "device-package credential meta-model name",
    # Certificates and signatures: public-key material, verifiable not usable.
    # Internal bookkeeping tokens (sequence ids, not credentials).
    ("pconsResolveCompleteRef", "token"): "policy-resolution sequence id (naming)",
    ("syntheticEpGroup", "acommunity"): "synthetic test class; BGP-style community attribute",
}


# ── The policy surface ────────────────────────────────────────────────────────


@cache
def _secure_flagged() -> frozenset[tuple[str, str]]:
    """Every (class, prop) the schema flags ``secure``, from the shipped db."""
    from niwaki.query import _catalog

    db = Path(_catalog.DEFAULT_PATH)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        (raw,) = con.execute("SELECT value FROM manifest WHERE key='prop_flags'").fetchone()
        secure_bit = 1 << str(raw).split(",").index("secure")
        return frozenset(
            (cls, wire)
            for cls, wire, flags in con.execute(
                "SELECT m.class_name, p.wire_name, p.flags FROM prop p JOIN mo m ON p.class_id=m.id"
            )
            if flags & secure_bit
        )
    finally:
        con.close()


def is_secret_prop(class_name: str, wire_name: str) -> bool:
    """``True`` when this property's value must never be published.

    The union of the schema's ``secure`` flag (mechanical — the APIC omits
    these from reads anyway) and the curated positions the flag misses
    (:data:`SECRET_VALUE_POSITIONS`).  DN-carried secrets are *not* reported
    here — a value redaction cannot reach them; see :func:`secret_dn_classes`.

    Args:
        class_name: Wire class name, e.g. ``"vnsCCred"``.
        wire_name: Wire property name, e.g. ``"value"``.
    """
    position = (class_name, wire_name)
    return position in SECRET_VALUE_POSITIONS or position in _secure_flagged()


def secret_dn_classes() -> frozenset[str]:
    """Classes whose DN itself carries a secret (a naming prop is secret).

    A consumer cannot redact these in place — the secret is in the RN, and in
    the DN of every descendant.  The choices are to warn, or to drop the
    subtree; silently publishing is the one wrong answer.
    """
    return frozenset(cls for cls, _prop in SECRET_DN_POSITIONS)
