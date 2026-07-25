# Credits

## Built with

### Runtime

- [Pydantic](https://docs.pydantic.dev/) — the typed models and validation
- [httpx](https://www.python-httpx.org/) — HTTP transport, sync and async
- [websockets](https://websockets.readthedocs.io/) — the object-subscription
  push channel
- [stamina](https://stamina.hynek.me/) — retry with backoff
- [PyYAML](https://pyyaml.org/) — the design vocabulary curation

### Code generation and packaging

- [Jinja2](https://jinja.palletsprojects.com/) — the model generator's
  templates
- [Hatchling](https://hatch.pypa.io/) — the build backend
- [uv](https://docs.astral.sh/uv/) — dependency management and execution

### Testing and quality

- [pytest](https://docs.pytest.org/) — the test runner, with
  [pytest-asyncio](https://pytest-asyncio.readthedocs.io/),
  [pytest-httpx](https://colin-b.github.io/pytest_httpx/), and
  [pytest-cov](https://pytest-cov.readthedocs.io/)
- [Sybil](https://sybil.readthedocs.io/) — the executable documentation
  (every Python fence in these docs runs as a test)
- [Ruff](https://docs.astral.sh/ruff/) — formatting and linting
- [mypy](https://mypy-lang.org/) and
  [Pyright](https://microsoft.github.io/pyright/) — strict type checking
  (with type stubs from [typeshed](https://github.com/python/typeshed))
- [python-dotenv](https://github.com/theskumar/python-dotenv) — lab
  credentials for the live integration suite

### Documentation

- [Sphinx](https://www.sphinx-doc.org/) — the documentation builder
- [Furo](https://pradyunsg.me/furo/) — the theme
- [MyST](https://myst-parser.readthedocs.io/) — Markdown source
- [sphinx-copybutton](https://sphinx-copybutton.readthedocs.io/) —
  copy-paste-ready snippets

Each dependency is distributed under its own license, carried in its own
distribution artifacts.

## Provenance and trademarks

Cisco, Cisco ACI and APIC are trademarks of Cisco Systems, Inc. This project
is an independent SDK, not affiliated with, endorsed or sponsored by Cisco
Systems, Inc.

All other product names, logos, and brands mentioned in this documentation
are property of their respective owners, and are used for identification
purposes only. Their use does not imply affiliation with or endorsement by
their holders — nor does crediting a library imply that its authors endorse
this project.

The ACI class and property metadata the package ships (names, types,
formats, labels, and descriptive text in the read catalogue, model
docstrings and reference documentation) is derived from the Cisco APIC
Management Information Model, remains the property of Cisco Systems, Inc.,
and is reproduced solely for interoperability and documentation. See the
`NOTICE` file shipped with the package for the authoritative statement.
