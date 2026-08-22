"""Properties of scripts and script numbers.

Run:

    uv run pytest tests/properties/test_script.py
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st

from pyzcash.errors import ZcashError
from pyzcash.script import (
    Script,
    address_from_script_pubkey,
    decode_script_num,
    encode_script_num,
    script_pubkey_for,
)
from tests.strategies import (
    networks,
    script_numbers,
    scripts,
    transparent_addresses,
)

if TYPE_CHECKING:
    from pyzcash.address import TransparentAddress
    from pyzcash.consensus import Network

# --- script numbers ---------------------------------------------------------


@given(script_numbers)
def test_script_numbers_round_trip(value: int) -> None:
    assert decode_script_num(encode_script_num(value), max_len=5) == value


@given(script_numbers)
def test_script_numbers_are_minimally_encoded(value: int) -> None:
    """A minimal encoding never ends in a byte that carries no information."""
    encoded = encode_script_num(value)
    if len(encoded) > 1:
        # The last byte holds magnitude bits, or exists only to carry the sign.
        assert encoded[-1] & 0x7F or encoded[-2] & 0x80


@given(script_numbers)
def test_zero_is_the_only_value_with_an_empty_encoding(value: int) -> None:
    assert (encode_script_num(value) == b"") == (value == 0)


# --- scripts ----------------------------------------------------------------


@given(scripts)
def test_scripts_round_trip_through_bytes(script: Script) -> None:
    assert Script.from_hex(script.to_hex()) == script


@given(scripts)
def test_a_built_script_always_parses(script: Script) -> None:
    """Anything this library can assemble, it can read back.

    This property found a real bug: Script.build would happily emit a bare
    OP_PUSHDATA1, which is a push prefix rather than a standalone opcode, and
    the result was a script that could never parse. build now refuses it.
    """
    assert isinstance(script.operations(), list)


@given(st.binary(max_size=512))
def test_parsing_arbitrary_bytes_as_a_script_never_leaks(data: bytes) -> None:
    """Arbitrary bytes either parse or raise a ZcashError, nothing else."""
    try:
        Script(data).operations()
    except ZcashError:
        return


# --- scripts and addresses --------------------------------------------------


@given(transparent_addresses)
def test_an_address_survives_the_trip_through_its_script(
    address: TransparentAddress,
) -> None:
    """Every transparent address is recoverable from the script that pays it."""
    script = script_pubkey_for(address)
    assert address_from_script_pubkey(script, address.network) == address


@given(scripts, networks)
def test_a_script_that_yields_an_address_pays_to_that_address(
    script: Script, network: Network
) -> None:
    """The two directions must agree about what an output pays.

    Scoped to scripts this library built, which always push minimally. The
    property is deliberately not claimed for arbitrary bytes: a P2PKH script
    that pushes its hash with OP_PUSHDATA1 instead of a direct push still pays
    to the same address, but re-encoding it yields the canonical minimal push
    and therefore different bytes. See test_script.py for that case.
    """
    address = address_from_script_pubkey(script, network)
    if address is None:
        return  # not a standard template, which is a normal thing to be
    assert script_pubkey_for(address) == script
