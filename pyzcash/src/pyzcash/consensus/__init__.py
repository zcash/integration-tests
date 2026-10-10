"""Consensus parameters: networks, upgrades, branch IDs, and amounts.

This layer knows which rules are in force at a given height on a given network,
and how much money a number represents. Everything above it (addresses,
transactions, digests) is parameterized by these.
"""

from __future__ import annotations

from pyzcash.consensus.amount import COIN, MAX_MONEY, ZERO, Zatoshi
from pyzcash.consensus.upgrades import (
    BlockHeight,
    Network,
    NetworkUpgrade,
    UnknownBranchIdError,
)

__all__ = [
    "COIN",
    "MAX_MONEY",
    "ZERO",
    "BlockHeight",
    "Network",
    "NetworkUpgrade",
    "UnknownBranchIdError",
    "Zatoshi",
]
