"""Properties of addresses.

Run:

    uv run pytest tests/properties/test_address.py
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pyzcash.address import (
    SaplingAddress,
    TexAddress,
    TransparentAddress,
    UnifiedAddress,
    parse_address,
)
from pyzcash.errors import ZcashError
from tests.strategies import (
    networks,
    sapling_addresses,
    tex_addresses,
    transparent_addresses,
    unified_addresses,
)

if TYPE_CHECKING:
    from pyzcash.consensus import Network

# --- round-trip -------------------------------------------------------------


@given(transparent_addresses)
def test_transparent_addresses_round_trip(address: TransparentAddress) -> None:
    encoded = address.encode()
    assert TransparentAddress.decode(encoded, address.network) == address
    assert parse_address(encoded, address.network) == address


@given(sapling_addresses)
def test_sapling_addresses_round_trip(address: SaplingAddress) -> None:
    encoded = address.encode()
    assert SaplingAddress.decode(encoded, address.network) == address
    assert parse_address(encoded, address.network) == address


@given(unified_addresses())
def test_unified_addresses_round_trip(address: UnifiedAddress) -> None:
    encoded = address.encode()
    assert UnifiedAddress.decode(encoded, address.network) == address
    assert parse_address(encoded, address.network) == address


@given(tex_addresses)
def test_tex_addresses_round_trip(address: TexAddress) -> None:
    encoded = address.encode()
    assert TexAddress.decode(encoded, address.network) == address
    assert parse_address(encoded, address.network) == address


# --- what the network means -------------------------------------------------


@given(sapling_addresses, networks)
def test_an_address_decodes_only_on_its_own_network(
    address: SaplingAddress, other: Network
) -> None:
    """A Sapling address names its network in its prefix, so a mismatch fails.

    This is what stops mainnet funds being sent to a testnet address by a caller
    who simply passed the wrong network.
    """
    encoded = address.encode()
    if other is address.network:
        assert SaplingAddress.decode(encoded, other) == address
        return
    with pytest.raises(ZcashError):
        SaplingAddress.decode(encoded, other)


@given(tex_addresses)
def test_a_tex_address_names_the_same_recipient_as_its_p2pkh_form(
    address: TexAddress,
) -> None:
    """ZIP 320's whole point: the funds land at the same P2PKH hash."""
    assert address.to_transparent().hash == address.hash


# --- robustness -------------------------------------------------------------


@given(st.text(max_size=128), networks)
def test_parsing_arbitrary_text_never_leaks_an_exception(
    text: str, network: Network
) -> None:
    """Arbitrary text either parses or raises a ZcashError, nothing else."""
    try:
        parse_address(text, network)
    except ZcashError:
        return
