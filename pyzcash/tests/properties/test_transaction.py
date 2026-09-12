"""Properties of the transaction model, including fuzzing against real bytes.

Run:

    uv run pytest tests/properties/test_transaction.py

The robustness properties here matter more than most. A transaction parser is
the library's widest attack surface: it is the one function that will be handed
bytes from the network, from a block explorer, from an untrusted peer. It has to
be total. It either produces a transaction, or it raises a ZcashError; it never
crashes the caller with an IndexError leaking out of a slice.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from pyzcash.errors import ZcashError
from pyzcash.transaction import Transaction, TxVersion
from tests.json_vectors import load_vectors
from tests.strategies import transactions

# Real transactions from the canonical corpus, to fuzz against.
CANONICAL_TXS = [
    bytes.fromhex(str(row["tx"]))
    for name in ("zip_0143", "zip_0243", "zip_0244")
    for row in load_vectors(name)
]

# The headers a transaction can legitimately begin with. Uniformly random bytes
# essentially never produce one, so without this the fuzzer never gets past the
# version check and the parser body is never reached.
VALID_HEADERS = [
    (
        (0x8000_0000 | version.number).to_bytes(4, "little")
        + version.version_group_id.to_bytes(4, "little")
        if version.overwintered
        else version.number.to_bytes(4, "little")
    )
    for version in TxVersion
]

headed_bytes = st.builds(
    lambda header, tail: header + tail,
    st.sampled_from(VALID_HEADERS),
    st.binary(max_size=256),
)


@st.composite
def mutated_canonical_transactions(draw: st.DrawFn) -> bytes:
    """A real transaction with a single byte replaced."""
    raw = bytearray(draw(st.sampled_from(CANONICAL_TXS)))
    index = draw(st.integers(min_value=0, max_value=len(raw) - 1))
    raw[index] = draw(st.integers(min_value=0, max_value=255))
    return bytes(raw)


# --- round-trip -------------------------------------------------------------


@given(transactions())
def test_transactions_round_trip(tx: Transaction) -> None:
    assert Transaction.from_bytes(tx.to_bytes()) == tx


@given(transactions())
def test_serialization_is_deterministic(tx: Transaction) -> None:
    """One transaction, one encoding. Otherwise it would have two txids."""
    assert tx.to_bytes() == tx.to_bytes()
    assert Transaction.from_bytes(tx.to_bytes()).to_bytes() == tx.to_bytes()


# --- robustness -------------------------------------------------------------


@given(st.binary(max_size=512))
def test_arbitrary_bytes_never_leak_an_exception(data: bytes) -> None:
    """The contract: it parses, or it raises a ZcashError. Nothing else."""
    try:
        tx = Transaction.from_bytes(data)
    except ZcashError:
        return
    assert tx.to_bytes() == data  # if it parsed, the parse was exact


@given(headed_bytes)
def test_bytes_behind_a_valid_header_never_leak_an_exception(
    data: bytes,
) -> None:
    """The same contract, but reaching past the version check into the body."""
    try:
        tx = Transaction.from_bytes(data)
    except ZcashError:
        return
    assert tx.to_bytes() == data


@given(mutated_canonical_transactions())
def test_a_corrupted_real_transaction_never_leaks_an_exception(
    data: bytes,
) -> None:
    """The deepest fuzz available: corrupt a real Sapling or Orchard bundle.

    Most single-byte mutations land in a ciphertext or a proof and still parse,
    so the parser runs all the way to the end before it meets anything wrong.
    That is what makes this corpus worth having.
    """
    try:
        tx = Transaction.from_bytes(data)
    except ZcashError:
        return
    assert tx.to_bytes() == data


def test_the_mutation_fuzz_actually_reaches_the_parser() -> None:
    """A fuzz property that never parses anything is vacuously true.

    Assert that most mutations really do parse, so the property above cannot
    quietly decay into "everything is rejected at the first byte".
    """
    parsed = 0
    for raw in CANONICAL_TXS:
        mutated = bytearray(raw)
        mutated[len(mutated) // 2] ^= 0xFF
        try:
            Transaction.from_bytes(bytes(mutated))
            parsed += 1
        except ZcashError:
            pass
    assert parsed > len(CANONICAL_TXS) // 2, (
        "almost every mutation is rejected outright, so the fuzz never reaches "
        "the body of the parser"
    )
