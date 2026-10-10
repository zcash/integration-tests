"""Properties of the encoding primitives.

Run:

    uv run pytest tests/properties/test_encoding.py

Three families of property appear across this package.

Round-trip: decoding an encoding returns the original. This is the workhorse,
but it is *not* sufficient on its own. A wrong-but-invertible transformation
round-trips perfectly, which is exactly how the F4Jumble split bug survived (see
test_canonical_vectors.py). Round-trip properties and cross-implementation
vectors catch different things, and neither replaces the other.

Canonicality: one value has exactly one encoding. Where this fails, a consensus
system has two byte strings that mean the same thing, and so two hashes for one
transaction.

Robustness: a parser handed arbitrary input either succeeds or raises a
ZcashError. It never leaks an IndexError or a struct.error through the
abstraction to crash a caller who was correctly catching the library's errors.
"""

from __future__ import annotations

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from pyzcash.encoding import (
    Bech32Encoding,
    Reader,
    Writer,
    b58check_decode,
    b58check_encode,
    bech32_decode,
    bech32_encode,
    f4jumble,
    f4jumble_inverse,
)
from pyzcash.errors import ZcashError
from tests.strategies import compact_sizes, f4jumble_messages, hrps

# --- CompactSize ------------------------------------------------------------


@given(compact_sizes)
def test_compact_size_round_trips(value: int) -> None:
    encoded = Writer().write_compact_size(value).to_bytes()
    assert Reader(encoded).read_compact_size() == value


@given(compact_sizes)
def test_only_the_canonical_compact_size_encoding_is_accepted(
    value: int,
) -> None:
    """One value, one encoding. Otherwise a transaction has two hashes."""
    canonical = Writer().write_compact_size(value).to_bytes()

    for prefix, width in ((0xFD, 2), (0xFE, 4), (0xFF, 8)):
        if value >= 1 << (8 * width):
            continue
        longer = bytes([prefix]) + value.to_bytes(width, "little")
        if longer == canonical:
            continue
        with pytest.raises(ZcashError):
            Reader(longer).read_compact_size()


# --- Reader -----------------------------------------------------------------


@given(st.binary(max_size=64))
def test_reading_past_the_end_raises_rather_than_returning_short(
    data: bytes,
) -> None:
    with pytest.raises(ZcashError):
        Reader(data).read(len(data) + 1)


# --- Base58Check ------------------------------------------------------------


@given(st.binary(max_size=256))
def test_base58check_round_trips(payload: bytes) -> None:
    assert b58check_decode(b58check_encode(payload)) == payload


@given(st.text(max_size=128))
def test_decoding_arbitrary_text_as_base58check_never_leaks(text: str) -> None:
    try:
        b58check_decode(text)
    except ZcashError:
        return


# --- Bech32 and Bech32m -----------------------------------------------------


@given(hrps, st.binary(max_size=256), st.sampled_from(list(Bech32Encoding)))
def test_bech32_round_trips(
    hrp: str, payload: bytes, encoding: Bech32Encoding
) -> None:
    decoded_hrp, decoded, decoded_encoding = bech32_decode(
        bech32_encode(hrp, payload, encoding)
    )
    assert decoded_hrp == hrp
    assert decoded == payload
    assert decoded_encoding is encoding


@given(hrps, st.binary(min_size=1, max_size=128), st.integers(0, 1000))
def test_a_corrupted_bech32_string_is_caught(
    hrp: str, payload: bytes, seed: int
) -> None:
    """Flipping one data character must be caught by the checksum."""
    encoded = bech32_encode(hrp, payload, Bech32Encoding.BECH32M)
    index = len(hrp) + 1 + (seed % (len(encoded) - len(hrp) - 1))
    replacement = "q" if encoded[index] != "q" else "p"
    corrupted = encoded[:index] + replacement + encoded[index + 1 :]
    assume(corrupted != encoded)

    try:
        _, decoded, _ = bech32_decode(corrupted)
    except ZcashError:
        return  # rejected, which is the point
    assert decoded != payload, "a corrupted string decoded to the same payload"


@given(st.text(max_size=128))
def test_decoding_arbitrary_text_as_bech32_never_leaks(text: str) -> None:
    try:
        bech32_decode(text)
    except ZcashError:
        return


# --- F4Jumble ---------------------------------------------------------------


@given(f4jumble_messages)
@settings(max_examples=50)  # each example is a handful of BLAKE2b compressions
def test_f4jumble_is_a_bijection(message: bytes) -> None:
    assert f4jumble_inverse(f4jumble(message)) == message
    assert f4jumble(f4jumble_inverse(message)) == message


@given(f4jumble_messages, st.integers(min_value=0))
@settings(max_examples=50)
def test_f4jumble_diffuses_every_byte(message: bytes, seed: int) -> None:
    """Change one input byte and essentially the whole output changes.

    This is the property unified addresses depend on: it is why a receiver
    cannot be stripped out of an address without destroying the whole payload.
    """
    index = seed % len(message)
    other = bytearray(message)
    other[index] ^= 0xFF

    a, b = f4jumble(message), f4jumble(bytes(other))
    differing = sum(x != y for x, y in zip(a, b, strict=True))
    assert differing > len(message) * 0.9
