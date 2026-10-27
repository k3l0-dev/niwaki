"""``NamedRegister`` and ``member_of`` on their own — the value and its validator.

The behaviour of the five register-backed model fields is pinned in
``test_named_register.py``; this module covers the two building blocks of
``niwaki.models._wire`` directly: construction and its refusals, the compact
decoding, value semantics (immutability, equality, hashing, a bounded
``repr``) and the before-validator on a small register.
"""

from __future__ import annotations

import dataclasses
from types import MappingProxyType
from typing import Annotated

import pytest
from pydantic import AfterValidator, BaseModel, BeforeValidator, ValidationError

from niwaki.models._wire import NamedRegister, member_of, named_number

_SMALL = NamedRegister.decode("0 1000 2000", "generic F1000 E2000", "F7")


# ── Construction ──────────────────────────────────────────────────────────────


def test_decode_pairs_numbers_with_names() -> None:
    assert _SMALL.names == frozenset({"generic", "F1000", "E2000", "F7"})
    assert dict(_SMALL.aliases) == {"0": "generic", "1000": "F1000", "2000": "E2000"}


def test_decode_tolerates_any_whitespace_and_line_breaks() -> None:
    register = NamedRegister.decode(" 0\n1000 ", "generic\tF1000")
    assert dict(register.aliases) == {"0": "generic", "1000": "F1000"}


def test_decode_without_aliases_holds_only_unaliased_names() -> None:
    register = NamedRegister.decode("", "", "a b")
    assert register.names == frozenset({"a", "b"})
    assert dict(register.aliases) == {}


@pytest.mark.parametrize(
    ("numbers", "names", "unaliased", "match"),
    [
        pytest.param("0 1", "generic", "", "alias key", id="unpaired"),
        pytest.param("0 0", "generic F1", "", "twice", id="repeated-key"),
        pytest.param("", "", "", "at least one name", id="empty"),
    ],
)
def test_decode_refuses_a_malformed_encoding(
    numbers: str, names: str, unaliased: str, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        NamedRegister.decode(numbers, names, unaliased)


def test_an_alias_to_an_undeclared_name_is_refused() -> None:
    with pytest.raises(ValueError, match="undeclared"):
        NamedRegister(names=frozenset({"generic"}), aliases={"1": "F1"})


def test_the_constructor_normalises_its_containers() -> None:
    source = {"0": "generic"}
    register = NamedRegister(names=frozenset({"generic"}), aliases=source)
    assert isinstance(register.names, frozenset)
    assert isinstance(register.aliases, MappingProxyType)
    source["1"] = "F1"  # the caller's dict is copied, not wrapped
    assert "1" not in register.aliases


# ── Value semantics ───────────────────────────────────────────────────────────


def test_a_register_is_immutable() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        _SMALL.names = frozenset()  # type: ignore[misc]
    with pytest.raises(TypeError):
        _SMALL.aliases["3"] = "F3"  # type: ignore[index]


def test_equal_content_is_equal_and_hashes_alike() -> None:
    same = NamedRegister.decode("2000 1000 0", "E2000 F1000 generic", "F7")
    assert same == _SMALL
    assert hash(same) == hash(_SMALL)
    assert len({same, _SMALL}) == 1


def test_different_content_is_not_equal() -> None:
    assert NamedRegister.decode("0 1000 2000", "generic F1000 E2000") != _SMALL
    assert NamedRegister.decode("0 1000 2000", "generic F1000 E2000", "F8") != _SMALL
    assert _SMALL != "generic"


def test_repr_never_lists_the_content() -> None:
    big = NamedRegister.decode(
        " ".join(map(str, range(5000))), " ".join(f"F{n}" for n in range(5000))
    )
    assert repr(big) == "NamedRegister(<5,000 names, 5,000 aliases>)"
    assert "F4999" not in repr(big)


# ── member_of, alone and in a field ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("given", "returned"),
    [
        ("F1000", "F1000"),
        ("F7", "F7"),
        ("1000", 1000),
        (" +1_000", 1000),
        ("1000.0", 1000),
        (1000, 1000),
        (1000.0, 1000),
        (True, 1),
        (b"1000", 1000),
        (-5, -5),
    ],
    ids=repr,
)
def test_member_of_returns_a_name_or_an_int(given: object, returned: object) -> None:
    value = member_of(_SMALL)(given)
    assert value == returned
    assert type(value) is type(returned)


@pytest.mark.parametrize(
    "given",
    ["F1", "f1000", " F1000", "", "0x3e8", "1e3", b"F1000", bytearray(b"1000"), None, 1.5, [1]],
    ids=repr,
)
def test_member_of_refuses_everything_else(given: object) -> None:
    with pytest.raises(ValueError, match="neither one of the 4 names"):
        member_of(_SMALL)(given)


def test_a_str_subclass_name_is_stored_as_a_plain_str() -> None:
    class Code(str):
        pass

    value = member_of(_SMALL)(Code("F1000"))
    assert value == "F1000"
    assert type(value) is str


class _Model(BaseModel):
    code: Annotated[
        int | str,
        BeforeValidator(member_of(_SMALL)),
        AfterValidator(named_number(_SMALL.aliases)),
        _SMALL,
    ] = "generic"


def test_a_register_backed_field_stores_names_and_refuses_the_rest() -> None:
    assert _Model(code="F7").code == "F7"
    assert _Model(code=1000).code == "F1000"
    assert _Model(code="2000").code == "E2000"
    assert _Model(code=5).code == 5
    with pytest.raises(ValidationError) as error:
        _Model(code="bogus")
    (detail,) = error.value.errors()
    assert detail["type"] == "value_error"
    assert "F1000" not in str(error.value)  # the register is never dumped


def test_the_field_carries_the_register_in_its_metadata() -> None:
    info = _Model.model_fields["code"]
    assert info.annotation == int | str
    assert [item for item in info.metadata if isinstance(item, NamedRegister)] == [_SMALL]
    assert any(item is _SMALL for item in info.metadata)
