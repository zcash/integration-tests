"""pyzcash: typed Zcash protocol primitives.

A dependency-free, fully typed library for working with Zcash data offline:
parsing and encoding addresses, scripts, and transactions, and computing the
digests the consensus rules define.

The library is built in layers, each usable on its own:

- :mod:`pyzcash.errors`      the exception hierarchy every failure derives from
- :mod:`pyzcash.encoding`    byte and string codecs (CompactSize, Base58Check,
                             Bech32/Bech32m, F4Jumble) and the hash functions
- :mod:`pyzcash.consensus`   networks, upgrades, branch IDs, and the Zatoshi
                             amount type
- :mod:`pyzcash.address`     transparent, Sapling, unified (ZIP 316), and TEX
                             (ZIP 320) addresses, as a tagged union

Layers still to land: scripts, transactions, digests (ZIP 143/243/244), and
fees (ZIP 317).

This library does not verify proofs, decrypt notes, or validate consensus. It
reads and writes the wire formats; it does not decide what is valid.
"""

from __future__ import annotations

from pyzcash.consensus import (
    COIN,
    MAX_MONEY,
    BlockHeight,
    Network,
    NetworkUpgrade,
    UnknownBranchIdError,
    Zatoshi,
)
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
    "COIN",
    "MAX_MONEY",
    "BlockHeight",
    "ChecksumError",
    "EncodingError",
    "Network",
    "NetworkUpgrade",
    "ParseError",
    "RangeError",
    "TrailingDataError",
    "TruncatedDataError",
    "UnknownBranchIdError",
    "Zatoshi",
    "ZcashError",
    "__version__",
]
