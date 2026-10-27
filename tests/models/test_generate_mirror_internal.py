"""Internal packages of the generated tree mirror to an empty placeholder.

The shared registers of giant named numbers live in an underscore-named
package (``_generated/_registers/__init__.py``).  The public mirror keeps one
file per generated module, but must not re-export implementation data: an
internal package mirrors to the bare generated banner, and the canonical
package may not promise a public name list (``__all__``) the placeholder
could not honour.  A public package with code but no short-name table is
still refused (``test_generate_mirror_errors.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from niwaki._codegen.generate_mirror import MirrorSourceError, render_mirror

_REGISTERS = (
    "from niwaki.models._wire import NamedRegister\n\n"
    "REGISTER_abc = NamedRegister.decode(numbers='0', names='generic', unaliased='')\n"
)


def _tree(root: Path, files: dict[str, str]) -> Path:
    gen = root / "_generated"
    gen.mkdir()
    (gen / "__init__.py").write_text("", encoding="utf-8")
    for rel, text in files.items():
        path = gen / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return gen


def test_an_internal_package_mirrors_to_an_empty_placeholder(tmp_path: Path) -> None:
    gen = _tree(tmp_path, {"_registers/__init__.py": _REGISTERS})
    text = render_mirror(gen)[Path("_registers") / "__init__.py"]
    assert text.startswith("# ruff: noqa\n")
    assert "do not edit manually" in text
    # Nothing but comments: no import, no name, no __all__.
    assert all(line.startswith("#") for line in text.splitlines() if line.strip())


def test_an_internal_package_with_a_public_name_list_is_refused(tmp_path: Path) -> None:
    gen = _tree(tmp_path, {"_registers/__init__.py": _REGISTERS + "__all__ = ['REGISTER_abc']\n"})
    with pytest.raises(MirrorSourceError, match="internal package"):
        render_mirror(gen)


def test_a_public_package_with_the_same_code_is_still_refused(tmp_path: Path) -> None:
    gen = _tree(tmp_path, {"registers/__init__.py": _REGISTERS})
    with pytest.raises(MirrorSourceError, match="no _CLASSES short-name table"):
        render_mirror(gen)


def test_the_real_registers_package_mirrors_to_a_placeholder() -> None:
    """The committed ``_registers`` package renders as the bare banner."""
    rendered = render_mirror()
    key = Path("_registers") / "__init__.py"
    assert key in rendered
    assert "NamedRegister" not in rendered[key]
    assert "REGISTER_" not in rendered[key]
