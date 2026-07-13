"""Sphinx configuration for the niwaki documentation.

Build locally (static HTML, no server needed):
    uv run sphinx-build -b html docs docs/_build/html
    open docs/_build/html/index.html
"""

from __future__ import annotations

from importlib.metadata import version as _pkg_version

from niwaki._codegen.generate_docs import curated_position_count

# ── Project ───────────────────────────────────────────────────────────────────

project = "niwaki"
author = "Khalid El-Ouiali"
copyright = "2026, Monark AIOPS SRL"
release = _pkg_version("niwaki")  # single source of truth: pyproject.toml
html_baseurl = "https://k3l0-dev.github.io/niwaki/"

# ── Extensions ────────────────────────────────────────────────────────────────

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "myst_parser",
    "sphinx_copybutton",
]

# Docstrings are Google-style (Args/Returns/Raises + Example:: blocks) with
# Sphinx cross-reference roles — Napoleon parses the sections, autodoc the rest.
napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_use_rtype = False

autodoc_member_order = "bysource"
autodoc_typehints = "description"
autodoc_typehints_description_target = "documented"
maximum_signature_line_length = 90

# One global policy instead of per-directive options (the source of silent
# omissions).  autodoc only touches the curated public surface — never the
# 2,222 generated model classes, whose fields are documented by the generated
# DSL reference (docs/reference/vocabulary/).
autodoc_default_options = {
    "members": True,
    "show-inheritance": True,
    "member-order": "bysource",
    # Pydantic machinery — noise on every model page.
    "exclude-members": "model_config,model_fields,model_computed_fields",
}

# "wiki" holds the GitHub-wiki signpost pages (published by
# scripts/publish_wiki.sh, not part of the Sphinx site).
exclude_patterns = ["_build", "wiki"]

myst_enable_extensions = ["colon_fence", "deflist", "fieldlist", "substitution"]
myst_heading_anchors = 3

# Generated figures the narrative pages cite without going stale:
# {{ positions }} always matches the generated coverage matrix.
myst_substitutions = {"positions": str(curated_position_count())}

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "pydantic": ("https://docs.pydantic.dev/latest", None),
}

# ── Cross-reference strictness ────────────────────────────────────────────────
# Nitpicky keeps the docstrings honest: any dead :class:/:meth: reference fails
# the build.  Private design/transport internals are documented by their public
# wrappers, not autodoc'd — ignore their targets explicitly.

nitpicky = True

# The generated cursors are documented by the DSL reference (one page per
# position), not by autodoc — their names appear in the factories' return
# annotations and in generated docstrings.  The ignore list below is DERIVED
# from the code, so it stays exact: a typo in a cursor name is not in the list
# and therefore fails the build.
from niwaki.design._generated_cursors import CURSOR_FOR  # noqa: E402

_CURSOR_CLASSES = {CURSOR_FOR[key] for key in CURSOR_FOR}
_CURSOR_IGNORES = [
    *(("py:class", cls.__name__) for cls in _CURSOR_CLASSES),
    *(("py:class", f"{cls.__module__}.{cls.__name__}") for cls in _CURSOR_CLASSES),
]

nitpick_ignore = [
    *_CURSOR_IGNORES,
    # Typing spellings autodoc cannot resolve to a documented target.
    ("py:class", "T"),
    ("py:class", "_T"),
    ("py:class", "_Coroutine"),
    ("py:class", "PushMode"),
    ("py:class", "niwaki.transport.session._T"),
    ("py:class", "niwaki.transport.session_async._T"),
    # pydantic's inventory does not expose its exceptions.
    ("py:exc", "pydantic.ValidationError"),
    ("py:exc", "ValidationError"),
    ("py:class", "pydantic.ValidationError"),
]
nitpick_ignore_regex = [
    # Design/facade/query internals: documented through their public wrappers.
    ("py:class", r"niwaki\.design\._.*"),
    ("py:mod", r"niwaki\.design\._.*"),
    ("py:class", r"niwaki\.facade\._.*"),
    ("py:class", r"niwaki\.query\._.*"),
    ("py:class", r"niwaki\.models\._generated\..*"),
]

# ── HTML output ───────────────────────────────────────────────────────────────

html_theme = "furo"
html_title = "niwaki — Cisco ACI SDK"
# _static carries the coverage badge endpoint (docs/_static/coverage-badge.json,
# refreshed by scripts/checks.sh) served from the Pages site.
html_static_path = ["_static"]
# The generated attribute tables are six columns wide; furo's default content
# column squeezes Cisco's descriptions into a few characters.
html_css_files = ["custom.css"]
html_theme_options = {
    "source_repository": "https://github.com/k3l0-dev/niwaki",
    "source_branch": "main",
    "source_directory": "docs/",
}

# Strip the ">>> " prompts when copying doctest-style blocks.
copybutton_prompt_text = ">>> "

# ── linkcheck (advisory: scripts/docs.sh linkcheck, non-blocking in CI) ───────

linkcheck_ignore = [
    r"https://apic\.example\.com.*",  # documentation placeholder host
    r"https://github\.com/k3l0-dev/niwaki/issues/new.*",  # requires auth
    r"https://github\.com/k3l0-dev/niwaki/security/.*",  # requires auth
]
linkcheck_allowed_redirects = {
    r"https://cobra\.readthedocs\.io.*": r".*",
    r"https://docs\.pydantic\.dev.*": r".*",
}
