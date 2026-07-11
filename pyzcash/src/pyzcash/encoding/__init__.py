"""Byte and string codecs, and the hash functions they are built from.

This is the bottom layer: it knows how Zcash writes bytes and strings, and
nothing about what those bytes mean. Addresses, scripts, and transactions are
all built on it.
"""

from __future__ import annotations

from pyzcash.encoding.base58 import (
    b58check_decode,
    b58check_encode,
    b58decode,
    b58encode,
)
from pyzcash.encoding.bech32 import (
    Bech32Encoding,
    bech32_decode,
    bech32_encode,
    convert_bits,
)
from pyzcash.encoding.f4jumble import (
    MAX_LENGTH,
    MIN_LENGTH,
    f4jumble,
    f4jumble_inverse,
)
from pyzcash.encoding.hashes import (
    blake2b_personal,
    blake2s_personal,
    hash160,
    ripemd160,
    sha256,
    sha256d,
)
from pyzcash.encoding.stream import Reader, Writer

__all__ = [
    "MAX_LENGTH",
    "MIN_LENGTH",
    "Bech32Encoding",
    "Reader",
    "Writer",
    "b58check_decode",
    "b58check_encode",
    "b58decode",
    "b58encode",
    "bech32_decode",
    "bech32_encode",
    "blake2b_personal",
    "blake2s_personal",
    "convert_bits",
    "f4jumble",
    "f4jumble_inverse",
    "hash160",
    "ripemd160",
    "sha256",
    "sha256d",
]
