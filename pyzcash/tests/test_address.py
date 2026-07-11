"""Addresses, checked against real ones from the suite's test fixtures."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pyzcash.address import (
    Receiver,
    ReceiverType,
    SaplingAddress,
    TexAddress,
    TransparentAddress,
    TransparentKind,
    UnifiedAddress,
    parse_address,
)
from pyzcash.encoding import Bech32Encoding, bech32_decode, bech32_encode
from pyzcash.errors import ChecksumError, ParseError
from tests.vectors import ALL_ADDRESSES

if TYPE_CHECKING:
    from pyzcash import Network
    from tests.conftest import MakeUnified


def test_parse_dispatches_to_the_right_type(
    regtest: Network,
    p2sh_address: str,
    sapling_address: str,
    unified_address: str,
) -> None:
    assert isinstance(parse_address(p2sh_address, regtest), TransparentAddress)
    assert isinstance(parse_address(sapling_address, regtest), SaplingAddress)
    assert isinstance(parse_address(unified_address, regtest), UnifiedAddress)


@pytest.mark.parametrize("encoded", ALL_ADDRESSES)
def test_round_trip_reproduces_the_exact_string(
    encoded: str, regtest: Network
) -> None:
    """Decode then encode is the identity, or we are rewriting addresses."""
    assert parse_address(encoded, regtest).encode() == encoded


def test_transparent_p2sh(regtest: Network, p2sh_address: str) -> None:
    address = TransparentAddress.decode(p2sh_address, regtest)
    assert address.kind is TransparentKind.P2SH
    assert len(address.hash) == 20


def test_sapling_splits_into_diversifier_and_transmission_key(
    regtest: Network, sapling_address: str
) -> None:
    address = SaplingAddress.decode(sapling_address, regtest)
    assert len(address.diversifier) == 11
    assert len(address.pk_d) == 32
    assert address.raw == address.diversifier + address.pk_d


def test_unified_address_receivers(
    regtest: Network, unified_address: str
) -> None:
    address = UnifiedAddress.decode(unified_address, regtest)
    assert address.has_orchard
    orchard = address.receiver(ReceiverType.ORCHARD)
    assert orchard is not None
    assert len(orchard.data) == 43
    # Whatever it carries, the ZIP 316 invariants must hold.
    typecodes = [r.typecode for r in address.receivers]
    assert typecodes == sorted(set(typecodes))


def test_a_unified_address_encoded_as_bech32_is_rejected(
    regtest: Network, unified_address: str
) -> None:
    """Sapling predates BIP 350 (Bech32); unified came after (Bech32m).

    A validly checksummed Bech32 string under the unified prefix must still be
    rejected, or the two checksum constants would be interchangeable and the
    distinction would buy nothing.
    """
    body = bech32_decode(unified_address)[1]
    wrong = bech32_encode("uregtest", body, Bech32Encoding.BECH32)
    with pytest.raises(ParseError, match="must be Bech32m"):
        UnifiedAddress.decode(wrong, regtest)


def test_a_sapling_address_encoded_as_bech32m_is_rejected(
    regtest: Network, sapling_address: str
) -> None:
    body = bech32_decode(sapling_address)[1]
    wrong = bech32_encode("zregtestsapling", body, Bech32Encoding.BECH32M)
    with pytest.raises(ParseError, match="must be Bech32, not Bech32m"):
        SaplingAddress.decode(wrong, regtest)


def test_the_network_is_checked(
    mainnet: Network, sapling_address: str, unified_address: str
) -> None:
    with pytest.raises(ParseError, match="expected a main"):
        SaplingAddress.decode(sapling_address, mainnet)
    with pytest.raises(ParseError, match="expected a main"):
        UnifiedAddress.decode(unified_address, mainnet)


def test_parse_explains_a_wrong_network(
    mainnet: Network, sapling_address: str
) -> None:
    with pytest.raises(ParseError):
        parse_address(sapling_address, mainnet)


def test_a_corrupted_checksum_is_reported_as_such(
    regtest: Network, p2sh_address: str
) -> None:
    """A mistyped address must not be reported as merely 'not an address'."""
    corrupted = p2sh_address[:-1] + ("A" if p2sh_address[-1] != "A" else "B")
    with pytest.raises(ChecksumError):
        parse_address(corrupted, regtest)


def test_tampering_with_a_unified_address_destroys_it(
    regtest: Network, unified_address: str
) -> None:
    """F4Jumble's purpose: you cannot edit part of a UA and keep it valid."""
    tail = "qq" if not unified_address.endswith("qq") else "pp"
    corrupted = unified_address[:-2] + tail
    with pytest.raises((ParseError, ChecksumError)):
        parse_address(corrupted, regtest)


# --- the ZIP 316 structural rules ------------------------------------------


def test_a_unified_address_must_have_a_shielded_receiver(
    make_unified: MakeUnified, p2pkh_receiver: Receiver
) -> None:
    """Transparent-only is what ZIP 320 TEX addresses are for."""
    with pytest.raises(ParseError, match="at least one shielded receiver"):
        make_unified(p2pkh_receiver)


def test_a_unified_address_has_at_most_one_transparent_receiver(
    make_unified: MakeUnified,
    p2pkh_receiver: Receiver,
    p2sh_receiver: Receiver,
    orchard_receiver: Receiver,
) -> None:
    with pytest.raises(ParseError, match="at most one transparent receiver"):
        make_unified(p2pkh_receiver, p2sh_receiver, orchard_receiver)


def test_receivers_must_be_in_ascending_typecode_order(
    make_unified: MakeUnified,
    sapling_receiver: Receiver,
    orchard_receiver: Receiver,
) -> None:
    with pytest.raises(ParseError, match="ascending typecode order"):
        make_unified(orchard_receiver, sapling_receiver)


def test_receivers_must_not_repeat_a_typecode(
    make_unified: MakeUnified, orchard_receiver: Receiver
) -> None:
    with pytest.raises(ParseError, match="must not repeat"):
        make_unified(orchard_receiver, orchard_receiver)


def test_a_unified_address_needs_at_least_one_receiver(
    make_unified: MakeUnified,
) -> None:
    with pytest.raises(ParseError, match="at least one receiver"):
        make_unified()


@pytest.mark.parametrize(
    ("receiver_type", "expected_len"),
    [
        (ReceiverType.ORCHARD, 43),
        (ReceiverType.SAPLING, 43),
        (ReceiverType.P2PKH, 20),
        (ReceiverType.P2SH, 20),
    ],
)
def test_a_receiver_of_the_wrong_length_is_rejected(
    receiver_type: ReceiverType, expected_len: int
) -> None:
    with pytest.raises(ParseError, match=f"is {expected_len} bytes"):
        Receiver(type=receiver_type, data=bytes(expected_len - 1))


def test_a_unified_address_round_trips_through_encoding(
    make_unified: MakeUnified,
    mainnet: Network,
    p2pkh_receiver: Receiver,
    sapling_receiver: Receiver,
    orchard_receiver: Receiver,
) -> None:
    address = make_unified(p2pkh_receiver, sapling_receiver, orchard_receiver)
    encoded = address.encode()
    assert encoded.startswith("u1")
    assert UnifiedAddress.decode(encoded, mainnet) == address


# --- TEX (ZIP 320) ---------------------------------------------------------


def test_tex_round_trips_and_maps_to_p2pkh(mainnet: Network) -> None:
    tex = TexAddress(network=mainnet, hash=bytes(range(20)))
    encoded = tex.encode()
    assert encoded.startswith("tex1")
    assert TexAddress.decode(encoded, mainnet) == tex

    equivalent = tex.to_transparent()
    assert equivalent.kind is TransparentKind.P2PKH
    assert equivalent.hash == tex.hash
    assert parse_address(encoded, mainnet) == tex
