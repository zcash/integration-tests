"""Bech32 (BIP 173) and Bech32m (BIP 350).

Sapling keys and addresses are Bech32; unified containers (ZIP 316) are
Bech32m. The two differ only in the checksum constant, so one decoder handles
both and reports which it found.

The test framework borrowed four primitives from the third-party ``embit``
package to build a decoder here, because embit's own decoder rejects strings
longer than 90 characters. That 90-character cap is a BIP 173 rule, and ZIP 316
explicitly lifts it: a unified address carrying several receivers is far longer.
This implementation has no length cap, which is what Zcash requires, so the
dependency is gone.
"""

from __future__ import annotations

from enum import Enum

from pyzcash.errors import ChecksumError, EncodingError

__all__ = ["Bech32Encoding", "bech32_decode", "bech32_encode", "convert_bits"]

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_CHARSET_INDEX = {c: i for i, c in enumerate(CHARSET)}
_SEPARATOR = "1"


class Bech32Encoding(Enum):
    """Which checksum constant an encoding uses."""

    BECH32 = 1
    """BIP 173. Used by Sapling addresses and keys."""

    BECH32M = 0x2BC8_30A3
    """BIP 350. Used by ZIP 316 unified containers."""


def _polymod(values: list[int]) -> int:
    generator = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for value in values:
        top = chk >> 25
        chk = ((chk & 0x1FF_FFFF) << 5) ^ value
        for i, g in enumerate(generator):
            if (top >> i) & 1:
                chk ^= g
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def convert_bits(
    data: bytes | list[int], from_bits: int, to_bits: int, pad: bool
) -> list[int]:
    """Regroup a bit stream from ``from_bits``-wide units to ``to_bits``-wide.

    Bech32 carries 5-bit groups but Zcash payloads are bytes, so every encode
    is an 8-to-5 regroup and every decode is a 5-to-8 regroup.

    Raises:
        EncodingError: on a value that does not fit ``from_bits``, or, when
            ``pad`` is false, on a non-zero or over-long remainder. That
            remainder check is what rejects a payload with tampered trailing
            bits whose checksum still happens to pass.
    """
    acc = 0
    bits = 0
    out: list[int] = []
    max_value = (1 << to_bits) - 1
    max_acc = (1 << (from_bits + to_bits - 1)) - 1
    for value in data:
        if value < 0 or (value >> from_bits):
            raise EncodingError(
                f"value {value} does not fit in {from_bits} bits"
            )
        acc = ((acc << from_bits) | value) & max_acc
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            out.append((acc >> bits) & max_value)
    if pad:
        if bits:
            out.append((acc << (to_bits - bits)) & max_value)
    elif bits >= from_bits or ((acc << (to_bits - bits)) & max_value):
        raise EncodingError("invalid padding in bit conversion")
    return out


def bech32_encode(
    hrp: str, payload: bytes, encoding: Bech32Encoding = Bech32Encoding.BECH32M
) -> str:
    """Encode ``payload`` under the human-readable prefix ``hrp``.

    Args:
        hrp: the human-readable part, e.g. ``"zs"`` or ``"u"``.
        payload: the raw bytes to carry.
        encoding: BECH32 for Sapling, BECH32M for unified containers.
    """
    if not hrp:
        raise EncodingError("the human-readable part cannot be empty")
    if any(ord(c) < 33 or ord(c) > 126 for c in hrp):
        raise EncodingError(
            f"the human-readable part has invalid characters: {hrp!r}"
        )
    if hrp != hrp.lower():
        raise EncodingError(
            f"the human-readable part must be lowercase: {hrp!r}"
        )

    data = convert_bits(payload, 8, 5, pad=True)
    values = _hrp_expand(hrp) + data
    polymod = _polymod([*values, 0, 0, 0, 0, 0, 0]) ^ encoding.value
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + _SEPARATOR + "".join(CHARSET[d] for d in data + checksum)


def bech32_decode(encoded: str) -> tuple[str, bytes, Bech32Encoding]:
    """Decode a Bech32 or Bech32m string.

    Returns:
        The human-readable part, the payload bytes, and which checksum
        constant validated. Callers that require one specific encoding (ZIP 316
        requires Bech32m) must check the returned encoding.

    Raises:
        EncodingError: if the string is malformed, mixed-case, or has no
            separator.
        ChecksumError: if neither checksum constant validates.
    """
    if any(ord(c) < 33 or ord(c) > 126 for c in encoded):
        raise EncodingError(
            "the string contains characters outside the printable range"
        )
    if encoded.lower() != encoded and encoded.upper() != encoded:
        raise EncodingError("the string mixes upper and lower case")
    encoded = encoded.lower()

    pos = encoded.rfind(_SEPARATOR)
    if pos < 1 or pos + 7 > len(encoded):
        raise EncodingError(
            "the string has no separator, or too short a data part after it"
        )
    hrp, data_part = encoded[:pos], encoded[pos + 1 :]

    values: list[int] = []
    for char in data_part:
        index = _CHARSET_INDEX.get(char)
        if index is None:
            raise EncodingError(f"invalid character {char!r} in the data part")
        values.append(index)

    checksum = _polymod(_hrp_expand(hrp) + values)
    for candidate in Bech32Encoding:
        if checksum == candidate.value:
            payload = convert_bits(values[:-6], 5, 8, pad=False)
            return hrp, bytes(payload), candidate
    raise ChecksumError(
        f"the checksum is not valid under Bech32 or Bech32m: {hrp!r}"
    )
