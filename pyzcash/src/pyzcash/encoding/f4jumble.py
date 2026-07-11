"""F4Jumble (ZIP 316), the unkeyed permutation over unified container payloads.

A unified address is a list of receivers. Without F4Jumble, a wallet that did
not recognise a receiver type could strip it out and still produce a
well-formed, checksum-valid address. F4Jumble makes every output byte depend on
every input byte, so any truncation corrupts the whole payload and the parse
fails instead of silently succeeding with fewer receivers.

It is a 4-round unbalanced Feistel network over two halves L and R:

    R ^= G(0, L);  L ^= H(0, R);  R ^= G(1, L);  L ^= H(1, R)

and the inverse undoes those four mixings in reverse. It is a permutation, not
encryption: there is no key and it provides no confidentiality.

The test framework had only the inverse, because decoding was all it needed.
Both directions are here, which is what makes the round-trip test possible.
"""

from __future__ import annotations

from pyzcash.encoding.hashes import blake2b_personal
from pyzcash.errors import ParseError

__all__ = ["MAX_LENGTH", "MIN_LENGTH", "f4jumble", "f4jumble_inverse"]

MIN_LENGTH = 48
"""The shortest payload ZIP 316 permits."""

MAX_LENGTH = 4194368
"""The longest payload ZIP 316 permits (2**22 + 64)."""

_H_PERSONAL = b"UA_F4Jumble_H"
_G_PERSONAL = b"UA_F4Jumble_G"


def _check_length(message: bytes) -> tuple[int, int]:
    length = len(message)
    if length < MIN_LENGTH or length > MAX_LENGTH:
        raise ParseError(
            f"F4Jumble requires {MIN_LENGTH} to {MAX_LENGTH} bytes, "
            f"got {length}"
        )
    left_len = (length + 1) // 2 if length <= 128 else 64
    return left_len, length - left_len


def _h(i: int, right: bytes, left_len: int) -> bytes:
    return blake2b_personal(
        _H_PERSONAL + bytes([i]) + b"\x00\x00", right, digest_size=left_len
    )


def _g(i: int, left: bytes, right_len: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < right_len:
        out += blake2b_personal(
            _G_PERSONAL + bytes([i]) + counter.to_bytes(2, "little"),
            left,
            digest_size=64,
        )
        counter += 1
    return bytes(out[:right_len])


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b, strict=True))


def f4jumble(message: bytes) -> bytes:
    """Apply F4Jumble.

    Raises:
        ParseError: if the message length is outside the range ZIP 316 permits.
    """
    left_len, right_len = _check_length(message)
    left, right = message[:left_len], message[left_len:]
    right = _xor(right, _g(0, left, right_len))
    left = _xor(left, _h(0, right, left_len))
    right = _xor(right, _g(1, left, right_len))
    left = _xor(left, _h(1, right, left_len))
    return left + right


def f4jumble_inverse(message: bytes) -> bytes:
    """Undo F4Jumble, recovering the receivers and their trailing padding.

    Raises:
        ParseError: if the message length is outside the range ZIP 316 permits.
    """
    left_len, right_len = _check_length(message)
    left, right = message[:left_len], message[left_len:]
    left = _xor(left, _h(1, right, left_len))
    right = _xor(right, _g(1, left, right_len))
    left = _xor(left, _h(0, right, left_len))
    right = _xor(right, _g(0, left, right_len))
    return left + right
