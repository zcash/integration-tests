"""Bech32/Bech32m and F4Jumble, checked against real Zcash encodings.

The end-to-end test here is the important one: it takes a unified full viewing
key that a real wallet produced, decodes it with this library's Bech32m, undoes
this library's F4Jumble, and asserts the receivers that fall out are the ones
that key actually carries. Bech32m, F4Jumble, and the CompactSize reader all
have to be right for that to hold: get any of them wrong and the padding check
fails or the receiver lengths come out as noise.
"""

from __future__ import annotations

import pytest

from pyzcash.encoding import (
    Bech32Encoding,
    Reader,
    bech32_decode,
    bech32_encode,
    f4jumble,
    f4jumble_inverse,
)
from pyzcash.errors import ChecksumError, EncodingError, ParseError
from tests.vectors import (
    ORCHARD_FVK_LEN,
    SAPLING_EXTFVK,
    SAPLING_EXTFVK_PAYLOAD_LEN,
    SAPLING_FVK_LEN,
    TRANSPARENT_FVK_LEN,
    TYPECODE_ORCHARD,
    TYPECODE_P2PKH,
    TYPECODE_SAPLING,
    UNIFIED_FVK,
    UNIFIED_FVK_HRP,
)

_PAD_LEN = 16


def test_sapling_extfvk_is_bech32_not_bech32m() -> None:
    hrp, payload, encoding = bech32_decode(SAPLING_EXTFVK)
    assert hrp == "zxviewregtestsapling"
    assert encoding is Bech32Encoding.BECH32
    assert len(payload) == SAPLING_EXTFVK_PAYLOAD_LEN


def test_unified_fvk_is_bech32m_not_bech32() -> None:
    hrp, _, encoding = bech32_decode(UNIFIED_FVK)
    assert hrp == UNIFIED_FVK_HRP
    assert encoding is Bech32Encoding.BECH32M


@pytest.mark.parametrize("encoded", [SAPLING_EXTFVK, UNIFIED_FVK])
def test_bech32_round_trip_reproduces_the_exact_string(encoded: str) -> None:
    hrp, payload, encoding = bech32_decode(encoded)
    assert bech32_encode(hrp, payload, encoding) == encoded


def test_decoding_a_unified_key_yields_the_expected_receivers() -> None:
    """The whole layer, end to end, against a key a real wallet produced."""
    hrp, payload, encoding = bech32_decode(UNIFIED_FVK)
    assert encoding is Bech32Encoding.BECH32M

    plaintext = f4jumble_inverse(payload)

    # ZIP 316 pads the payload with the HRP, zero-extended to 16 bytes.
    padding = plaintext[-_PAD_LEN:]
    assert padding == hrp.encode() + b"\x00" * (_PAD_LEN - len(hrp))

    # What remains is a list of (typecode, length, data) receivers.
    reader = Reader(plaintext[:-_PAD_LEN])
    receivers: dict[int, bytes] = {}
    while reader.remaining:
        typecode = reader.read_compact_size()
        receivers[typecode] = reader.read_bytes_compact()

    assert set(receivers) == {
        TYPECODE_P2PKH,
        TYPECODE_SAPLING,
        TYPECODE_ORCHARD,
    }
    assert len(receivers[TYPECODE_P2PKH]) == TRANSPARENT_FVK_LEN
    assert len(receivers[TYPECODE_SAPLING]) == SAPLING_FVK_LEN
    assert len(receivers[TYPECODE_ORCHARD]) == ORCHARD_FVK_LEN

    # Receivers are in ascending typecode order, as ZIP 316 requires.
    assert list(receivers) == sorted(receivers)


def test_f4jumble_round_trips() -> None:
    for length in (48, 49, 127, 128, 129, 200):
        message = bytes((i * 7 + length) % 256 for i in range(length))
        assert f4jumble_inverse(f4jumble(message)) == message


def test_f4jumble_diffuses_every_byte() -> None:
    """Flipping one input bit must change essentially the whole output.

    This is the property the unified-address format depends on: it is why a
    receiver cannot be stripped from an address without destroying it.
    """
    a = bytes(64)
    b = bytes([1]) + bytes(63)
    ja, jb = f4jumble(a), f4jumble(b)
    differing = sum(x != y for x, y in zip(ja, jb, strict=True))
    assert differing > 60


@pytest.mark.parametrize("length", [0, 47, 4194369])
def test_f4jumble_rejects_lengths_outside_zip316(length: int) -> None:
    with pytest.raises(ParseError):
        f4jumble(bytes(length))


def test_bech32_rejects_a_corrupted_checksum() -> None:
    corrupted = UNIFIED_FVK[:-1] + ("q" if UNIFIED_FVK[-1] != "q" else "p")
    with pytest.raises(ChecksumError):
        bech32_decode(corrupted)


def test_bech32_rejects_mixed_case() -> None:
    with pytest.raises(EncodingError, match="mixes upper and lower case"):
        bech32_decode(SAPLING_EXTFVK[:-4] + SAPLING_EXTFVK[-4:].upper())


def test_bech32_has_no_ninety_character_limit() -> None:
    """BIP 173 caps strings at 90 characters; ZIP 316 lifts that cap.

    This is exactly why the test framework could not use embit's decoder.
    """
    assert len(UNIFIED_FVK) > 90
    bech32_decode(UNIFIED_FVK)  # must not raise
