"""Base58Check, the encoding of transparent addresses and WIF keys.

The test framework never had this: it reached for the third-party ``base58``
package in one test. It is forty lines, so the library owns it and stays
dependency-free.
"""

from __future__ import annotations

from pyzcash.encoding.hashes import sha256d
from pyzcash.errors import ChecksumError, EncodingError

__all__ = ["b58check_decode", "b58check_encode", "b58decode", "b58encode"]

_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}
_CHECKSUM_LEN = 4


def b58encode(data: bytes) -> str:
    """Encode bytes as Base58 (no checksum)."""
    n = int.from_bytes(data, "big")
    out = bytearray()
    while n > 0:
        n, rem = divmod(n, 58)
        out.append(_ALPHABET[rem])
    # Each leading zero byte encodes as a leading '1', which the integer
    # conversion above cannot represent.
    for byte in data:
        if byte != 0:
            break
        out.append(_ALPHABET[0])
    return bytes(reversed(out)).decode("ascii")


def b58decode(encoded: str) -> bytes:
    """Decode a Base58 string (no checksum).

    Raises:
        EncodingError: if the string contains a character outside the alphabet.
    """
    n = 0
    for byte in encoded.encode("ascii", errors="replace"):
        digit = _INDEX.get(byte)
        if digit is None:
            raise EncodingError(
                f"invalid Base58 character {chr(byte)!r} in {encoded!r}"
            )
        n = n * 58 + digit

    # A leading zero byte encodes as a leading '1', which the integer
    # conversion cannot round-trip, so restore one per leading '1'.
    leading_zeros = len(encoded) - len(encoded.lstrip("1"))

    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * leading_zeros + body


def b58check_encode(payload: bytes) -> str:
    """Encode ``payload`` with a 4-byte double-SHA-256 checksum appended."""
    return b58encode(payload + sha256d(payload)[:_CHECKSUM_LEN])


def b58check_decode(encoded: str) -> bytes:
    """Decode a Base58Check string and verify its checksum.

    Returns:
        The payload, with the checksum stripped.

    Raises:
        EncodingError: if the string is not valid Base58 or is too short.
        ChecksumError: if the checksum does not match the payload.
    """
    raw = b58decode(encoded)
    if len(raw) < _CHECKSUM_LEN:
        raise EncodingError(
            f"Base58Check string is too short to carry a checksum: {encoded!r}"
        )
    payload, checksum = raw[:-_CHECKSUM_LEN], raw[-_CHECKSUM_LEN:]
    expected = sha256d(payload)[:_CHECKSUM_LEN]
    if checksum != expected:
        raise ChecksumError(
            f"Base58Check checksum mismatch: expected {expected.hex()}, "
            f"got {checksum.hex()}"
        )
    return payload
