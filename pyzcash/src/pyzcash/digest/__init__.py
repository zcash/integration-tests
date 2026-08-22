"""Transaction digests: txids, auth digests, and sighashes.

A transaction has more than one hash, and they mean different things.

- The *txid* is its identity. For v5 (ZIP 244) it is a digest of what the
  transaction does, so it does not change when the signatures do. For
  earlier
  versions it is a double SHA-256 of the serialized bytes, which is exactly the
  malleability ZIP 244 was written to remove.
- The *auth digest* commits to the proofs and signatures that the txid leaves
  out, so they are still bound to the chain.
- A *sighash* is what a signature is actually made over. It is not the txid: it
  depends on which input is being signed and on the sighash type, and it commits
  to the value of the output being spent, which the transaction itself does not
  contain.

Every digest here is a personalized BLAKE2b. The personalization is the whole
security argument: two digests over identical bytes still differ if their
personalizations do, so a value computed for one purpose can never be
substituted for another.
"""

from __future__ import annotations

from pyzcash.digest import zip143, zip244
from pyzcash.digest.sighash_type import (
    SIGHASH_ALL,
    SIGHASH_NONE,
    SIGHASH_SINGLE,
    SigHashType,
    SigHashUnit,
)
from pyzcash.digest.zip143 import SigningInput
from pyzcash.digest.zip244 import (
    PrevOutput,
    auth_digest,
    orchard_digest,
    sapling_digest,
    transparent_digest,
    txid_digest,
)

__all__ = [
    "SIGHASH_ALL",
    "SIGHASH_NONE",
    "SIGHASH_SINGLE",
    "PrevOutput",
    "SigHashType",
    "SigHashUnit",
    "SigningInput",
    "auth_digest",
    "orchard_digest",
    "sapling_digest",
    "transparent_digest",
    "txid_digest",
    "zip143",
    "zip244",
]
