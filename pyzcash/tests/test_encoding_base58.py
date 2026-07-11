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
from tests.vectors import BASE58_PAYLOADS


@pytest.mark.parametrize("payload", BASE58_PAYLOADS)
def test_b58encode_matches_the_reference_implementation(payload: bytes) -> None:
    assert b58encode(payload) == reference.b58encode(payload).decode()


@pytest.mark.parametrize("payload", BASE58_PAYLOADS)
def test_b58check_matches_the_reference_implementation(payload: bytes) -> None:
    assert (
        b58check_encode(payload) == reference.b58encode_check(payload).decode()
    )


@pytest.mark.parametrize("payload", BASE58_PAYLOADS)
def test_b58check_round_trips(payload: bytes) -> None:
    assert b58check_decode(b58check_encode(payload)) == payload


@pytest.mark.parametrize("payload", BASE58_PAYLOADS)
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
