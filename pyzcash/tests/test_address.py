"""Addresses, checked against real ones from the suite's test fixtures."""

from __future__ import annotations

import pytest

from pyzcash import Network
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

# Real regtest addresses, lifted from qa/rpc-tests/test-wallet/.
P2SH = "t2AELYrVCe7Cy2tdFz8XfWpG42ps3zJm39K"
SAPLING = (
    "zregtestsapling1235ejypsw2f0mwesy0ygtp4tknrm2znypkurm8wac25yywgdfzrgcr0dre"
    "udf96whh47s52fz6n"
)
UNIFIED = (
    "uregtest10l3s7t08y44grzpr462x6kmvt3g4nsd8gmy69ags72c9hdr28t62k3fg9e5x8ayfu"
    "5cg2rvwk4ytm06fqvs092zeytx9f5kcer38lztz50janztazqxq0vn5jxll8tjqshsvexqdxpw"
    "2548x9j6y0cm2vw9dud30khupf57gj7tk6eyrxxf7ycvaddv9gyrjal0s8dpnafl7qghqat7"
)


def test_parse_dispatches_to_the_right_type() -> None:
    assert isinstance(parse_address(P2SH, Network.REGTEST), TransparentAddress)
    assert isinstance(parse_address(SAPLING, Network.REGTEST), SaplingAddress)
    assert isinstance(parse_address(UNIFIED, Network.REGTEST), UnifiedAddress)


@pytest.mark.parametrize("encoded", [P2SH, SAPLING, UNIFIED])
def test_round_trip_reproduces_the_exact_string(encoded: str) -> None:
    """Decode then encode is the identity, or we are rewriting addresses."""
    assert parse_address(encoded, Network.REGTEST).encode() == encoded


def test_transparent_p2sh() -> None:
    address = TransparentAddress.decode(P2SH, Network.REGTEST)
    assert address.kind is TransparentKind.P2SH
    assert len(address.hash) == 20


def test_sapling_splits_into_diversifier_and_transmission_key() -> None:
    address = SaplingAddress.decode(SAPLING, Network.REGTEST)
    assert len(address.diversifier) == 11
    assert len(address.pk_d) == 32
    assert address.raw == address.diversifier + address.pk_d


def test_unified_address_receivers() -> None:
    address = UnifiedAddress.decode(UNIFIED, Network.REGTEST)
    assert address.has_orchard
    orchard = address.receiver(ReceiverType.ORCHARD)
    assert orchard is not None
    assert len(orchard.data) == 43
    # Whatever it carries, the ZIP 316 invariants must hold.
    typecodes = [r.typecode for r in address.receivers]
    assert typecodes == sorted(set(typecodes))


def test_a_unified_address_encoded_as_bech32_is_rejected() -> None:
    """Sapling predates BIP 350 (Bech32); unified came after (Bech32m).

    A validly checksummed Bech32 string under the unified prefix must still be
    rejected, or the two checksum constants would be interchangeable and the
    distinction would buy nothing.
    """
    body = bech32_decode(UNIFIED)[1]
    wrong = bech32_encode("uregtest", body, Bech32Encoding.BECH32)
    with pytest.raises(ParseError, match="must be Bech32m"):
        UnifiedAddress.decode(wrong, Network.REGTEST)


def test_a_sapling_address_encoded_as_bech32m_is_rejected() -> None:
    body = bech32_decode(SAPLING)[1]
    wrong = bech32_encode("zregtestsapling", body, Bech32Encoding.BECH32M)
    with pytest.raises(ParseError, match="must be Bech32, not Bech32m"):
        SaplingAddress.decode(wrong, Network.REGTEST)


def test_the_network_is_checked() -> None:
    with pytest.raises(ParseError, match="expected a main"):
        SaplingAddress.decode(SAPLING, Network.MAIN)
    with pytest.raises(ParseError, match="expected a main"):
        UnifiedAddress.decode(UNIFIED, Network.MAIN)


def test_parse_explains_a_wrong_network() -> None:
    with pytest.raises(ParseError):
        parse_address(SAPLING, Network.MAIN)


def test_a_corrupted_checksum_is_rejected() -> None:
    corrupted = P2SH[:-1] + ("A" if P2SH[-1] != "A" else "B")
    with pytest.raises(ChecksumError):
        parse_address(corrupted, Network.REGTEST)


def test_tampering_with_a_unified_address_destroys_it() -> None:
    """F4Jumble's purpose: you cannot edit part of a UA and keep it valid."""
    corrupted = UNIFIED[:-2] + ("qq" if not UNIFIED.endswith("qq") else "pp")
    with pytest.raises((ParseError, ChecksumError)):
        parse_address(corrupted, Network.REGTEST)


# --- the ZIP 316 structural rules ------------------------------------------

ORCHARD = Receiver(type=ReceiverType.ORCHARD, data=bytes(43))
SAPLING_R = Receiver(type=ReceiverType.SAPLING, data=bytes(43))
P2PKH_R = Receiver(type=ReceiverType.P2PKH, data=bytes(20))
P2SH_R = Receiver(type=ReceiverType.P2SH, data=bytes(20))


def test_a_unified_address_must_have_a_shielded_receiver() -> None:
    """Transparent-only is what ZIP 320 TEX addresses are for."""
    with pytest.raises(ParseError, match="at least one shielded receiver"):
        UnifiedAddress(network=Network.MAIN, receivers=(P2PKH_R,))


def test_a_unified_address_has_at_most_one_transparent_receiver() -> None:
    with pytest.raises(ParseError, match="at most one transparent receiver"):
        UnifiedAddress(
            network=Network.MAIN, receivers=(P2PKH_R, P2SH_R, ORCHARD)
        )


def test_receivers_must_be_ordered_and_unique() -> None:
    with pytest.raises(ParseError, match="ascending typecode order"):
        UnifiedAddress(network=Network.MAIN, receivers=(ORCHARD, SAPLING_R))
    with pytest.raises(ParseError, match="must not repeat"):
        UnifiedAddress(network=Network.MAIN, receivers=(ORCHARD, ORCHARD))


def test_a_receiver_of_the_wrong_length_is_rejected() -> None:
    with pytest.raises(ParseError, match="ORCHARD receiver is 43 bytes"):
        Receiver(type=ReceiverType.ORCHARD, data=bytes(42))


def test_a_unified_address_round_trips_through_encoding() -> None:
    address = UnifiedAddress(
        network=Network.MAIN, receivers=(P2PKH_R, SAPLING_R, ORCHARD)
    )
    encoded = address.encode()
    assert encoded.startswith("u1")
    assert UnifiedAddress.decode(encoded, Network.MAIN) == address


# --- TEX (ZIP 320) ---------------------------------------------------------


def test_tex_round_trips_and_maps_to_p2pkh() -> None:
    tex = TexAddress(network=Network.MAIN, hash=bytes(range(20)))
    encoded = tex.encode()
    assert encoded.startswith("tex1")
    assert TexAddress.decode(encoded, Network.MAIN) == tex

    equivalent = tex.to_transparent()
    assert equivalent.kind is TransparentKind.P2PKH
    assert equivalent.hash == tex.hash
    assert parse_address(encoded, Network.MAIN) == tex
