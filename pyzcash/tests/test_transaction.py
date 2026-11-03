"""Transactions, against the canonical zcash-test-vectors corpus.

The regression suite here is every transaction in the ZIP 143, ZIP 243, and
ZIP 244 vector files: real v3, v4, and v5 transactions carrying JoinSplits,
Sapling bundles, and Orchard bundles. Each one must parse and then re-serialize
to the identical bytes. That single property is a strong one, because it fails
if any field is read at the wrong offset, in the wrong order, or with the wrong
width, anywhere in the transaction.

Run:

    uv run pytest tests/test_transaction.py
"""

from __future__ import annotations

import pytest

from pyzcash.consensus import Zatoshi
from pyzcash.errors import ParseError, TrailingDataError, TruncatedDataError
from pyzcash.script import Script
from pyzcash.transaction import (
    OrchardBundle,
    OutPoint,
    SaplingBundle,
    Transaction,
    TxIn,
    TxOut,
    TxVersion,
)
from tests.json_vectors import load_vectors

# The transaction-bearing vector files, and the version each one exercises.
ZIP143 = [row["tx"] for row in load_vectors("zip_0143")]  # v3, Overwinter
ZIP243 = [row["tx"] for row in load_vectors("zip_0243")]  # v4, Sapling
ZIP244 = [row["tx"] for row in load_vectors("zip_0244")]  # v5, ZIP 225
ALL_TXS = ZIP143 + ZIP243 + ZIP244

# A real v1 transaction, from qa/rpc-tests/decodescript.py.
V1_TX = (
    "0100000001696a20784a2c70143f634e95227dbdfdf0ecd51647052e70854512235f5986"
    "ca010000008a47304402207174775824bec6c2700023309a168231ec80b82c6069282f51"
    "33e6f11cbb04460220570edc55c7c5da2ca687ebd0372d3546ebc3f810516a002350cac7"
    "2dfe192dfb014104d3f898e6487787910a690410b7a917ef198905c27fb9d3b0a42da12a"
    "ceae0544fc7088d239d9a48f2828a15a09e84043001f27cc80d162cb95404e1210161536"
    "ffffffff0100e1f505000000001976a914eb6c6e0cdb2d256a32d97b8df1fc75d1920d9b"
    "ca88ac00000000"
)


# --- the regression suite ---------------------------------------------------


@pytest.mark.parametrize("tx_hex", ALL_TXS)
def test_every_canonical_transaction_round_trips(tx_hex: object) -> None:
    """Parse and re-serialize every vector transaction, byte for byte.

    This is the property that matters: it fails if any field is read at the
    wrong offset, in the wrong order, or with the wrong width.
    """
    assert isinstance(tx_hex, str)
    assert Transaction.from_hex(tx_hex).to_hex() == tx_hex


@pytest.mark.parametrize(
    ("vectors", "expected"),
    [
        (ZIP143, TxVersion.V3),
        (ZIP243, TxVersion.V4),
        (ZIP244, TxVersion.V5),
    ],
    ids=["zip_0143_v3", "zip_0243_v4", "zip_0244_v5"],
)
def test_each_vector_file_carries_the_version_it_is_about(
    vectors: list[object], expected: TxVersion
) -> None:
    for tx_hex in vectors:
        assert isinstance(tx_hex, str)
        assert Transaction.from_hex(tx_hex).version is expected


def test_the_corpus_actually_exercises_the_shielded_pools() -> None:
    """A round-trip suite proves little if every vector is a bare v1 payment.

    Assert the corpus really does contain JoinSplits, Sapling bundles, and
    Orchard bundles, so the round-trip property above is covering them.
    """
    txs = [Transaction.from_hex(h) for h in ALL_TXS if isinstance(h, str)]
    assert any(tx.joinsplits for tx in txs), "no JoinSplit coverage"
    assert any(tx.sapling_bundle.spends for tx in txs), "no Sapling spends"
    assert any(tx.sapling_bundle.outputs for tx in txs), "no Sapling outputs"
    assert any(tx.orchard_bundle.actions for tx in txs), "no Orchard actions"
    assert any(tx.vin for tx in txs), "no transparent inputs"
    assert any(tx.vout for tx in txs), "no transparent outputs"


def test_a_v1_transaction_round_trips_and_hashes() -> None:
    tx = Transaction.from_hex(V1_TX)
    assert tx.version is TxVersion.V1
    assert tx.to_hex() == V1_TX
    assert len(tx.vin) == 1
    assert len(tx.vout) == 1
    assert tx.vout[0].value == Zatoshi(100_000_000)
    # The txid is the double SHA-256 of the bytes, displayed byte-reversed.
    assert len(tx.legacy_txid()) == 32
    assert tx.legacy_txid_hex() == tx.legacy_txid()[::-1].hex()


# --- what the versions mean -------------------------------------------------


def test_a_v5_txid_is_not_a_hash_of_the_serialization() -> None:
    """ZIP 244 made the txid a digest of the transaction's effects.

    Returning sha256d of the bytes here would be plausible and wrong, so it
    raises instead.
    """
    tx = Transaction.from_hex(str(ZIP244[0]))
    with pytest.raises(ParseError, match="ZIP 244 digest"):
        tx.legacy_txid()


def test_an_unknown_version_is_refused_rather_than_guessed() -> None:
    """A v6 transaction is not a v1 transaction with a strange header.

    The framework's parser fell through to the legacy layout for anything it
    did not recognize, which would misread every byte after the header.
    """
    # Overwintered, version 6, with an unassigned version group ID.
    header = (0x8000_0006).to_bytes(4, "little") + bytes.fromhex("deadbeef")
    with pytest.raises(ParseError, match="unknown transaction version"):
        Transaction.from_bytes(header + bytes(16))


def test_the_version_group_id_is_part_of_the_version() -> None:
    """A v4 version number with the wrong group ID is not a Sapling tx."""
    with pytest.raises(ParseError, match="unknown transaction version"):
        TxVersion.identify(True, 0x0BADF00D, 4)


def test_joinsplit_proof_system_follows_the_version() -> None:
    """It is not recorded in the JoinSplit; v4 is Groth16, earlier is PHGR13."""
    assert TxVersion.V4.joinsplits_use_groth16
    assert not TxVersion.V3.joinsplits_use_groth16
    assert not TxVersion.V2.joinsplits_use_groth16
    assert TxVersion.V2.has_joinsplits
    assert not TxVersion.V1.has_joinsplits
    assert not TxVersion.V5.has_joinsplits


# --- parsing is exact -------------------------------------------------------


def test_trailing_bytes_are_an_error() -> None:
    """A transaction plus junk is not a transaction."""
    with pytest.raises(TrailingDataError):
        Transaction.from_hex(V1_TX + "00")


def test_a_truncated_transaction_is_an_error() -> None:
    with pytest.raises(TruncatedDataError):
        Transaction.from_hex(V1_TX[:-8])


# --- building ---------------------------------------------------------------


def test_a_hand_built_transaction_round_trips() -> None:
    tx = Transaction(
        version=TxVersion.V5,
        consensus_branch_id=0xC2D6_D0B4,  # NU5
        lock_time=0,
        expiry_height=100,
        vin=(
            TxIn(
                prevout=OutPoint(txid=bytes(range(32)), index=0),
                script_sig=Script(b"\x51"),
            ),
        ),
        vout=(TxOut(value=Zatoshi(1000), script_pubkey=Script(b"\x51")),),
        sapling_bundle=SaplingBundle(),
        orchard_bundle=OrchardBundle(),
    )
    assert Transaction.from_hex(tx.to_hex()) == tx


def test_an_empty_shielded_bundle_serializes_compactly() -> None:
    """An empty Orchard bundle is a single zero byte, not a full structure."""
    tx = Transaction(
        version=TxVersion.V5,
        consensus_branch_id=0xC2D6_D0B4,
        expiry_height=0,
    )
    raw = tx.to_bytes()
    # header(4) + group id(4) + branch id(4) + locktime(4) + expiry(4)
    # + vin(1) + vout(1) + sapling spends(1) + sapling outputs(1) + orchard(1)
    assert len(raw) == 25


def test_a_coinbase_input_is_recognized() -> None:
    coinbase = TxIn(prevout=OutPoint(txid=bytes(32), index=0xFFFFFFFF))
    tx = Transaction(version=TxVersion.V1, vin=(coinbase,))
    assert tx.is_coinbase
    assert coinbase.prevout.is_coinbase


def test_a_v1_transaction_cannot_carry_a_sapling_bundle() -> None:
    """The writer refuses to emit something no version can express."""
    tx = Transaction.from_hex(str(ZIP243[0]))
    impossible = Transaction(
        version=TxVersion.V1, sapling_bundle=tx.sapling_bundle
    )
    with pytest.raises(ParseError, match="cannot carry a Sapling bundle"):
        impossible.to_bytes()


def test_outpoint_txid_display_is_byte_reversed() -> None:
    """Internal order is what the wire carries; explorers show the reverse."""
    outpoint = OutPoint(txid=bytes(range(32)), index=0)
    assert outpoint.txid_hex() == bytes(reversed(range(32))).hex()
