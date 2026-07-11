"""Typed readers and writers for the consensus byte encoding.

The framework this code was extracted from spelled these as free functions over
a file object (``deser_uint256(f)``, ``ser_compactsize(n)``), with three
different CompactSize implementations that disagreed about canonicality. Here
there is one :class:`Reader` and one :class:`Writer`, they are typed, and they
enforce the rules the consensus encoding actually has.
"""

from __future__ import annotations

from typing import Self

from pyzcash.errors import ParseError, TrailingDataError, TruncatedDataError

__all__ = ["Reader", "Writer"]

_MAX_U64 = (1 << 64) - 1


class Reader:
    """A cursor over a byte string that raises rather than reading past the end.

    Example:
        >>> r = Reader(bytes.fromhex("0102"))
        >>> r.read_u8()
        1
        >>> r.expect_exhausted()  # 1 byte is left, so this raises
        Traceback (most recent call last):
        pyzcash.errors.TrailingDataError
    """

    __slots__ = ("_data", "_pos")

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    @property
    def position(self) -> int:
        """The number of bytes consumed so far."""
        return self._pos

    @property
    def remaining(self) -> int:
        """The number of bytes not yet consumed."""
        return len(self._data) - self._pos

    def read(self, n: int) -> bytes:
        """Read exactly ``n`` bytes, or raise :class:`TruncatedDataError`."""
        if n < 0:
            raise ValueError(f"cannot read a negative number of bytes: {n}")
        if n > self.remaining:
            raise TruncatedDataError(wanted=n, available=self.remaining)
        out = self._data[self._pos : self._pos + n]
        self._pos += n
        return out

    def read_u8(self) -> int:
        return self.read(1)[0]

    def read_u16(self) -> int:
        return int.from_bytes(self.read(2), "little")

    def read_u32(self) -> int:
        return int.from_bytes(self.read(4), "little")

    def read_u64(self) -> int:
        return int.from_bytes(self.read(8), "little")

    def read_i64(self) -> int:
        return int.from_bytes(self.read(8), "little", signed=True)

    def read_u256(self) -> bytes:
        """Read a 32-byte little-endian value, as bytes in wire order."""
        return self.read(32)

    def read_compact_size(self) -> int:
        """Read a CompactSize, rejecting non-canonical encodings.

        A value must use the shortest form that can express it. Bitcoin and
        Zcash both require this, and accepting the long forms would let two
        distinct byte strings parse to the same object, which breaks the
        round-trip guarantee the transaction types depend on.
        """
        first = self.read_u8()
        if first < 0xFD:
            return first
        if first == 0xFD:
            value = self.read_u16()
            threshold = 0xFD
        elif first == 0xFE:
            value = self.read_u32()
            threshold = 0x10000
        else:
            value = self.read_u64()
            threshold = 0x100000000
        if value < threshold:
            raise ParseError(
                f"non-canonical CompactSize: {value} was encoded with prefix "
                f"0x{first:02x}, but fits in a shorter form"
            )
        return value

    def read_bytes_compact(self) -> bytes:
        """Read a CompactSize length followed by that many bytes."""
        return self.read(self.read_compact_size())

    def expect_exhausted(self) -> None:
        """Raise :class:`TrailingDataError` unless every byte was consumed.

        Consensus encodings are exact, so a parser that leaves bytes behind has
        been handed something other than what the caller believed.
        """
        if self.remaining:
            raise TrailingDataError(remaining=self.remaining)


class Writer:
    """An append-only byte buffer, the inverse of :class:`Reader`."""

    __slots__ = ("_chunks",)

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def write(self, data: bytes) -> Self:
        self._chunks.append(data)
        return self

    def write_u8(self, value: int) -> Self:
        return self.write(self._checked(value, 1).to_bytes(1, "little"))

    def write_u16(self, value: int) -> Self:
        return self.write(self._checked(value, 2).to_bytes(2, "little"))

    def write_u32(self, value: int) -> Self:
        return self.write(self._checked(value, 4).to_bytes(4, "little"))

    def write_u64(self, value: int) -> Self:
        return self.write(self._checked(value, 8).to_bytes(8, "little"))

    def write_i64(self, value: int) -> Self:
        return self.write(value.to_bytes(8, "little", signed=True))

    def write_u256(self, value: bytes) -> Self:
        if len(value) != 32:
            raise ValueError(f"expected 32 bytes, got {len(value)}")
        return self.write(value)

    def write_compact_size(self, value: int) -> Self:
        """Write a CompactSize in its canonical (shortest) form."""
        if value < 0 or value > _MAX_U64:
            raise ValueError(f"CompactSize out of range: {value}")
        if value < 0xFD:
            return self.write(bytes([value]))
        if value <= 0xFFFF:
            return self.write(b"\xfd" + value.to_bytes(2, "little"))
        if value <= 0xFFFFFFFF:
            return self.write(b"\xfe" + value.to_bytes(4, "little"))
        return self.write(b"\xff" + value.to_bytes(8, "little"))

    def write_bytes_compact(self, data: bytes) -> Self:
        """Write a CompactSize length followed by the bytes themselves."""
        return self.write_compact_size(len(data)).write(data)

    def to_bytes(self) -> bytes:
        return b"".join(self._chunks)

    def __len__(self) -> int:
        return sum(len(c) for c in self._chunks)

    @staticmethod
    def _checked(value: int, size: int) -> int:
        if value < 0 or value >= (1 << (8 * size)):
            raise ValueError(f"value {value} does not fit in {size} byte(s)")
        return value
