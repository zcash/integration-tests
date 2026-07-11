"""Base58 and Base58Check.

The library implements these itself so the core stays dependency-free. To make
sure that reimplementation is right rather than merely self-consistent, it is
differential-tested against the reference `base58` package, which is a dev-only
dependency.
"""

from __future__ import annotations

import base58 as reference
import pytest

from pyzcash.encoding import (
    b58check_decode,
    b58check_encode,
    b58decode,
    b58encode,
)
from pyzcash.errors import ChecksumError, EncodingError

# Transparent address payloads are a 2-byte version prefix and a 20-byte hash;
# the leading-zero cases matter because they encode as leading '1' characters
# and are the classic place a Base58 implementation goes wrong.
PAYLOADS = [
    b"",
    b"\x00",
    b"\x00\x00\x00",
    b"\x00\x01\x02",
    bytes(range(22)),
    b"\x1c\xb8" + bytes(range(20)),  # a mainnet t-address shaped payload
    b"\xff" * 32,
]


@pytest.mark.parametrize("payload", PAYLOADS)
def test_b58encode_matches_the_reference_implementation(payload: bytes) -> None:
    assert b58encode(payload) == reference.b58encode(payload).decode()


@pytest.mark.parametrize("payload", PAYLOADS)
def test_b58check_matches_the_reference_implementation(payload: bytes) -> None:
    assert (
        b58check_encode(payload) == reference.b58encode_check(payload).decode()
    )


@pytest.mark.parametrize("payload", PAYLOADS)
def test_b58check_round_trips(payload: bytes) -> None:
    assert b58check_decode(b58check_encode(payload)) == payload


@pytest.mark.parametrize("payload", PAYLOADS)
def test_b58_round_trips(payload: bytes) -> None:
    assert b58decode(b58encode(payload)) == payload


def test_b58check_rejects_a_corrupted_checksum() -> None:
    encoded = b58check_encode(b"\x1c\xb8" + bytes(range(20)))
    corrupted = encoded[:-1] + ("A" if encoded[-1] != "A" else "B")
    with pytest.raises(ChecksumError):
        b58check_decode(corrupted)


def test_b58decode_rejects_characters_outside_the_alphabet() -> None:
    # '0', 'O', 'I' and 'l' are excluded from the alphabet by design.
    with pytest.raises(EncodingError, match="invalid Base58 character"):
        b58decode("0OIl")


def test_b58check_rejects_a_string_too_short_for_a_checksum() -> None:
    with pytest.raises(EncodingError, match="too short"):
        b58check_decode("11")
