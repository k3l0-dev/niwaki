"""Sphinx configuration for the niwaki documentation.

Build locally (static HTML, no server needed):
    uv run sphinx-build -b html docs docs/_build/html
    open docs/_build/html/index.html
"""

from __future__ import annotations

# ── Project ───────────────────────────────────────────────────────────────────

project = "niwaki"
author = "Khalid El-Ouiali"
copyright = "2026, Monark AIOP SRL"  # noqa: A001
release = "0.2.0"

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

# The generated model modules re-export ManagedObject subclasses; autodoc only
# touches the curated public surface, never the 2,222 generated classes.
autodoc_default_options = {
    "show-inheritance": True,
}

exclude_patterns = ["_build", "internal"]

myst_enable_extensions = ["colon_fence", "deflist", "fieldlist"]
myst_heading_anchors = 3

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "pydantic": ("https://docs.pydantic.dev/latest", None),
}

# ── Cross-reference strictness ────────────────────────────────────────────────
# Nitpicky keeps the docstrings honest: any dead :class:/:meth: reference fails
# the build.  Private design/transport internals are documented by their public
# wrappers, not autodoc'd — ignore their targets explicitly.

nitpicky = True
nitpick_ignore_regex = [
    # Design/facade/query internals: documented through their public wrappers.
    ("py:class", r"niwaki\.design\._.*"),
    ("py:mod", r"niwaki\.design\._.*"),
    ("py:class", r"niwaki\.facade\._.*"),
    ("py:class", r"niwaki\.query\._.*"),
    ("py:class", r"niwaki\.models\._generated\..*"),
    # Sessions are managed by the clients; only the protocols are documented.
    ("py:class", r"(niwaki\.transport\.)?(session(_async)?\.)?(Async)?ApicSession"),
    # TypeVars and typing spellings autodoc cannot resolve.
    ("py:class", r"^[TUM]$"),
    ("py:class", r"^_T$"),
    ("py:class", r"^_Coroutine$"),
    ("py:class", r"^PushMode$"),
    # Generated cursor types surface in factory return annotations.
    ("py:class", r"(niwaki\.design\.(_generated_cursors\.)?)?[A-Z][A-Za-z]*Cursor"),
    # pydantic's inventory does not expose its exceptions.
    ("py:exc", r"(pydantic\.)?ValidationError"),
    ("py:class", r"(pydantic\.)?ValidationError"),
    ("py:obj", r".*"),
]

# ── HTML output ───────────────────────────────────────────────────────────────

html_theme = "furo"
html_title = "niwaki — Cisco ACI SDK"
html_static_path: list[str] = []
html_theme_options = {
    "source_repository": "https://github.com/k3l0-dev/niwaki",
    "source_branch": "main",
    "source_directory": "docs/",
}

# Strip the ">>> " prompts when copying doctest-style blocks.
copybutton_prompt_text = ">>> "
