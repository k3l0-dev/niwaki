# Security Policy

niwaki talks to network controllers with administrative credentials — we
take reports seriously and appreciate coordinated disclosure.

## Supported versions

| Version | Supported |
| --- | --- |
| latest 0.x release | ✅ |
| anything older | ❌ — upgrade first |

## Reporting a vulnerability

**Never open a public issue for a vulnerability.**

- Preferred: [GitHub private vulnerability reporting](https://github.com/k3l0-dev/niwaki/security/advisories/new)
  (Security tab → *Report a vulnerability*).
- Alternatively: email **monark.aiops@pm.me** with the details.

You will get an acknowledgement within **72 hours**, a severity assessment,
and coordinated disclosure once a fix ships.  Credit is given unless you
prefer otherwise.

## Scope — what we care about most

- Credential handling: anything that could log, leak or persist APIC
  credentials or session tokens.
- Transport security: TLS verification (`verify_ssl` defaults to `true`),
  token refresh, session lifecycle.
- Injection: crafted names/DNs/filters reaching APIC API paths or query
  strings in unintended ways.
- Anything that could make the SDK **write** where the caller did not
  declare it.

## Posture

The SDK never logs credentials; tokens live in memory only.  Be aware that
`to_payload()` and `plan` outputs contain **your configuration** — redact
them before sharing anywhere public.
