"""Reader and Writer, and the CompactSize canonicality rule.

Run:

    uv run pytest tests/test_encoding_stream.py
"""

from __future__ import annotations

import pytest

from pyzcash.encoding import Reader, Writer
from pyzcash.errors import ParseError, TrailingDataError, TruncatedDataError


@pytest.mark.parametrize(
    ("value", "encoded"),
    [
        (0, "00"),
        (0xFC, "fc"),
        (0xFD, "fdfd00"),
        (0xFFFF, "fdffff"),
        (0x10000, "fe00000100"),
        (0xFFFFFFFF, "feffffffff"),
        (0x100000000, "ff0000000001000000"),
    ],
)
def test_compact_size_uses_the_canonical_form(value: int, encoded: str) -> None:
    assert Writer().write_compact_size(value).to_bytes().hex() == encoded
    assert Reader(bytes.fromhex(encoded)).read_compact_size() == value


@pytest.mark.parametrize(
    "encoded",
    [
        "fd0000",  # 0 in the 3-byte form
        "fdfc00",  # 0xfc in the 3-byte form
        "fe00000000",  # 0 in the 5-byte form
        "feffff0000",  # 0xffff in the 5-byte form
        "ff0000000000000000",  # 0 in the 9-byte form
    ],
)
def test_compact_size_rejects_non_canonical_encodings(encoded: str) -> None:
    """Two byte strings must never parse to the same value.

    The test framework accepted these long forms, which silently breaks the
    round-trip guarantee the transaction types rely on.
    """
    with pytest.raises(ParseError, match="non-canonical"):
        Reader(bytes.fromhex(encoded)).read_compact_size()


def test_reader_raises_rather_than_reading_past_the_end() -> None:
    reader = Reader(b"\x01\x02")
    with pytest.raises(TruncatedDataError) as excinfo:
        reader.read(4)
    assert excinfo.value.wanted == 4
    assert excinfo.value.available == 2


def test_expect_exhausted_rejects_trailing_data() -> None:
    reader = Reader(b"\x01\x02")
    assert reader.read_u8() == 1
    with pytest.raises(TrailingDataError) as excinfo:
        reader.expect_exhausted()
    assert excinfo.value.remaining == 1


def test_integer_round_trips() -> None:
    writer = (
        Writer()
        .write_u8(0xFF)
        .write_u16(0xBEEF)
        .write_u32(0xDEADBEEF)
        .write_u64(2**63)
        .write_i64(-1)
        .write_u256(bytes(range(32)))
    )
    reader = Reader(writer.to_bytes())
    assert reader.read_u8() == 0xFF
    assert reader.read_u16() == 0xBEEF
    assert reader.read_u32() == 0xDEADBEEF
    assert reader.read_u64() == 2**63
    assert reader.read_i64() == -1
    assert reader.read_u256() == bytes(range(32))
    reader.expect_exhausted()


def test_length_prefixed_bytes_round_trip() -> None:
    short = bytes(range(200))
    encoded = Writer().write_bytes_compact(short).to_bytes()
    assert encoded[0] == 200  # under 0xfd, so the length is a single byte
    assert Reader(encoded).read_bytes_compact() == short

    long = bytes(300)
    encoded = Writer().write_bytes_compact(long).to_bytes()
    assert encoded[:3].hex() == "fd2c01"  # 300, in the canonical 3-byte form
    assert Reader(encoded).read_bytes_compact() == long


def test_writers_reject_out_of_range_values() -> None:
    with pytest.raises(ValueError, match="does not fit"):
        Writer().write_u8(256)
    with pytest.raises(ValueError, match="does not fit"):
        Writer().write_u32(-1)
