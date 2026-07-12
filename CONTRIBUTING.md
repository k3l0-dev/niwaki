# Contributing to niwaki

Thank you for helping tend the tree. 🌳

## How this repository works

niwaki is developed in a private workshop; the release engineering lives
there.  **This public repository is the product's home**: source, the full
test suite (14,200+ unit tests plus the executable documentation — run it
with `uv run pytest --ignore=tests/integration tests/ docs/`), documentation,
releases, issues and discussions.  Every release lands here as a single
commit, which is why pull requests are never merged directly — see
[the reference-patch model](#pull-requests-the-reference-patch-model) below.
The live integration walkthrough needs a lab APIC and skips itself without
one.

## The best ways to contribute

### 1. Report a bug

Use the bug report form.  A great report for a network SDK includes the
niwaki / Python / APIC versions, a **minimal design snippet** that reproduces
the problem, the `to_payload()` or `push(aci, mode="plan")` output, and the
full traceback.

> **Redact first.**  Never paste real credentials, IP addresses, hostnames or
> sensitive DNs into an issue.  The bug form makes you confirm this.

### 2. Request vocabulary curation — the most valuable contribution

The design DSL grows by curating positions: makers, `bind()` aliases and
verbs for the ACI classes operators actually use.  You know better than
anyone which classes your automation needs (ESG, VMM domains, L3Out
internals, FEX…).  Open a **vocabulary request** with the ACI class, where it
lives, the operator word you would expect to type, and your use case — this
is, quite literally, the project's roadmap funnel.

### 3. Propose a feature

Use the feature request form.  Explain the operational problem before the
solution — the design DSL has strong conventions (structure is literal,
verbatim is translated, closed-world references) and proposals that fit them
travel fastest.

### 4. Improve the documentation

The documentation lives at <https://k3l0-dev.github.io/niwaki/> and is built
from the `docs/` sources in this repository.  Typos, unclear guides, missing
examples: open an issue or a small PR.

The documentation is **executable**: every ` ```python ` fence runs as a
test (`uv run pytest docs/`), with blocks on the same page sharing one
namespace (a page reads as one continuous program).  When contributing
snippets: write blocks that really run against the SDK; mark the rare
non-runnable block (another SDK, live-fabric-only steps) with an HTML
comment `<!--- skip: next --->` on the line above the fence; and use
`text` / `console` fences for output that is shown, not executed.

## Pull requests: the reference-patch model

Pull requests are welcome as **reference patches**:

1. Open the PR as usual — the public CI (lint, types, tests, docs, packaging) runs
   on it.
2. A maintainer re-lands accepted changes in the private workshop, where the
   full test suite and the live APIC walkthrough validate them.
3. Your PR is closed with a reference to the release that ships the change,
   and **you are credited in the CHANGELOG**.

Keep patches focused and small, match the code style you see (typed, verbose
public docstrings, English), and never include secrets or environment
details.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).

## Security

Never report vulnerabilities in public issues — see [SECURITY.md](SECURITY.md).
