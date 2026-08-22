"""The transaction model: v1 through v5 (ZIP 225).

Parsing is exact and total. A transaction either round-trips to the identical
bytes or the parse raises. An unknown version is an error rather than a guess,
and trailing bytes are an error rather than something to ignore, because both of
those are ways to quietly misreport what a transaction says.

This library reads and writes transactions. It does not verify their proofs or
signatures, and it does not decide whether they are valid.
"""

from __future__ import annotations

from pyzcash.transaction.components import (
    SEQUENCE_FINAL,
    OutPoint,
    TxIn,
    TxOut,
)
from pyzcash.transaction.orchard import (
    OrchardAction,
    OrchardBundle,
    OrchardFlags,
)
from pyzcash.transaction.sapling import (
    SaplingBundle,
    SaplingOutput,
    SaplingSpend,
)
from pyzcash.transaction.sprout import (
    Groth16Proof,
    JoinSplit,
    PHGRProof,
    SproutProof,
)
from pyzcash.transaction.transaction import (
    OVERWINTER_VERSION_GROUP_ID,
    SAPLING_VERSION_GROUP_ID,
    SPROUT_VERSION_GROUP_ID,
    ZIP225_VERSION_GROUP_ID,
    Transaction,
    TxVersion,
)

__all__ = [
    "OVERWINTER_VERSION_GROUP_ID",
    "SAPLING_VERSION_GROUP_ID",
    "SEQUENCE_FINAL",
    "SPROUT_VERSION_GROUP_ID",
    "ZIP225_VERSION_GROUP_ID",
    "Groth16Proof",
    "JoinSplit",
    "OrchardAction",
    "OrchardBundle",
    "OrchardFlags",
    "OutPoint",
    "PHGRProof",
    "SaplingBundle",
    "SaplingOutput",
    "SaplingSpend",
    "SproutProof",
    "Transaction",
    "TxIn",
    "TxOut",
    "TxVersion",
]
