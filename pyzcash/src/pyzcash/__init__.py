"""pyzcash: typed Zcash protocol primitives.

A dependency-free, fully typed library for working with Zcash data offline:
parsing and encoding addresses, scripts, and transactions, and computing the
digests the consensus rules define.

The library is built in layers, each usable on its own:

- :mod:`pyzcash.errors`      the exception hierarchy every failure derives from
- :mod:`pyzcash.encoding`    byte and string codecs (CompactSize, Base58Check,
                             Bech32/Bech32m, F4Jumble) and the hash functions

Layers still to land: consensus parameters, amounts, addresses, scripts,
transactions, digests (ZIP 143/243/244), and fees (ZIP 317).

This library does not verify proofs, decrypt notes, or validate consensus. It
reads and writes the wire formats; it does not decide what is valid.
"""

from __future__ import annotations

from pyzcash.errors import (
    ChecksumError,
    EncodingError,
    ParseError,
    RangeError,
    TrailingDataError,
    TruncatedDataError,
    ZcashError,
)

__version__ = "0.0.1"

__all__ = [
    "ChecksumError",
    "EncodingError",
    "ParseError",
    "RangeError",
    "TrailingDataError",
    "TruncatedDataError",
    "ZcashError",
    "__version__",
]
