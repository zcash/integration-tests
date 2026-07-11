"""Script numbers.

Script integers are little-endian, sign-and-magnitude (the top bit of the last
byte is the sign), and minimally encoded: zero is the empty string, and no value
carries a redundant leading zero byte.

The test framework had three independent implementations of this: bignum.bn2vch,
script.CScriptNum.encode, and blocktools.serialize_script_num. There is one
here, and it round-trips.
"""

from __future__ import annotations

from pyzcash.errors import ParseError

__all__ = ["decode_script_num", "encode_script_num"]

DEFAULT_MAX_LEN = 4
"""Arithmetic opcodes operate on numbers of at most four bytes."""


def encode_script_num(value: int) -> bytes:
    """Encode ``value`` in the minimal script-number form."""
    if value == 0:
        return b""

    negative = value < 0
    magnitude = abs(value)

    out = bytearray()
    while magnitude:
        out.append(magnitude & 0xFF)
        magnitude >>= 8

    # The top bit of the last byte is the sign bit. If the magnitude already
    # uses it, a byte has to be added to carry the sign; otherwise the sign
    # goes into the spare bit.
    if out[-1] & 0x80:
        out.append(0x80 if negative else 0x00)
    elif negative:
        out[-1] |= 0x80

    return bytes(out)


def decode_script_num(data: bytes, max_len: int = DEFAULT_MAX_LEN) -> int:
    """Decode a script number, requiring the minimal encoding.

    Raises:
        ParseError: if the value is longer than ``max_len``, or is not minimally
            encoded. A non-minimal encoding is rejected because it would give a
            number two representations, and consensus rules that hash scripts
            cannot tolerate that.
    """
    if not data:
        return 0
    if len(data) > max_len:
        raise ParseError(
            f"script number is {len(data)} bytes, at most {max_len} allowed"
        )

    # Minimality: the last byte must carry magnitude bits of its own, unless it
    # exists only to hold the sign for a value whose top magnitude bit is set.
    if not data[-1] & 0x7F and (len(data) <= 1 or not data[-2] & 0x80):
        raise ParseError("script number is not minimally encoded")

    magnitude = 0
    for i, byte in enumerate(data):
        magnitude |= byte << (8 * i)

    sign_bit = 0x80 << (8 * (len(data) - 1))
    if magnitude & sign_bit:
        return -(magnitude & ~sign_bit)
    return magnitude
