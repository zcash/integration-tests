"""Txids, auth digests, and sighashes, against the canonical vectors.

Run:

    uv run pytest tests/test_digest.py

There is nothing to check these against except the canonical corpus. A digest
has no round-trip property, and no invariant a test could assert on its own: it
is either the same 32 bytes the rest of Zcash computes, or it is wrong. That
makes these vectors the only real test, and a complete one, since a single wrong
byte anywhere in the tree changes the result.
"""

from __future__ import annotations

import pytest

from pyzcash.consensus import Zatoshi
from pyzcash.digest import (
    PrevOutput,
    SigHashType,
    SigHashUnit,
    SigningInput,
    auth_digest,
    txid_digest,
    zip143,
    zip244,
)
from pyzcash.errors import ParseError
from pyzcash.script import Script
from pyzcash.transaction import Transaction
from tests.json_vectors import load_vectors


def _prev_outputs(vector: dict[str, object]) -> list[PrevOutput]:
    """The previous output of every transparent input the vector describes.

    Empty for a coinbase transaction, which spends nothing, and for one with no
    transparent inputs at all.
    """
    amounts = vector["amounts"]
    scripts = vector["script_pubkeys"]
    assert isinstance(amounts, list)
    assert isinstance(scripts, list)
    return [
        PrevOutput(
            value=Zatoshi(int(amount)),
            script_pubkey=Script(bytes.fromhex(str(script))),
        )
        for amount, script in zip(amounts, scripts, strict=True)
    ]


ZIP143 = load_vectors("zip_0143")  # v3, Overwinter
ZIP243 = load_vectors("zip_0243")  # v4, Sapling
ZIP244 = load_vectors("zip_0244")  # v5, ZIP 225

# The sighash variants ZIP 244's vectors record, and the type each one is.
SIGHASH_VARIANTS = [
    ("sighash_all", SigHashType(SigHashUnit.ALL)),
    ("sighash_none", SigHashType(SigHashUnit.NONE)),
    ("sighash_single", SigHashType(SigHashUnit.SINGLE)),
    ("sighash_all_anyone", SigHashType(SigHashUnit.ALL, anyone_can_pay=True)),
    ("sighash_none_anyone", SigHashType(SigHashUnit.NONE, anyone_can_pay=True)),
    (
        "sighash_single_anyone",
        SigHashType(SigHashUnit.SINGLE, anyone_can_pay=True),
    ),
]


# --- ZIP 244: txid and auth digest ------------------------------------------


@pytest.mark.parametrize("vector", ZIP244, ids=range(len(ZIP244)))
def test_zip244_txid(vector: dict[str, object]) -> None:
    tx = Transaction.from_hex(str(vector["tx"]))
    assert txid_digest(tx).hex() == str(vector["txid"])


@pytest.mark.parametrize("vector", ZIP244, ids=range(len(ZIP244)))
def test_zip244_auth_digest(vector: dict[str, object]) -> None:
    tx = Transaction.from_hex(str(vector["tx"]))
    assert auth_digest(tx).hex() == str(vector["auth_digest"])


def test_a_v5_txid_does_not_change_when_the_signatures_do() -> None:
    """The point of ZIP 244: identity is separate from authorization.

    Rewrite every transparent signature in a transaction and the txid must not
    move. Before ZIP 244 it would have, which is precisely the malleability the
    proposal was written to remove.
    """
    signed = [
        Transaction.from_hex(str(v["tx"]))
        for v in ZIP244
        if Transaction.from_hex(str(v["tx"])).vin
    ]
    assert signed, "no vector with a transparent input to re-sign"

    from dataclasses import replace

    from pyzcash.script import Script

    tx = signed[0]
    tampered = replace(
        tx,
        vin=tuple(
            replace(txin, script_sig=Script(b"\x51\x51")) for txin in tx.vin
        ),
    )
    assert txid_digest(tampered) == txid_digest(tx)
    # The auth digest, on the other hand, is exactly what must change.
    assert auth_digest(tampered) != auth_digest(tx)


# --- ZIP 244: sighashes -----------------------------------------------------


@pytest.mark.parametrize("vector", ZIP244, ids=range(len(ZIP244)))
def test_zip244_shielded_sighash(vector: dict[str, object]) -> None:
    """A shielded signature commits to the transparent bundle as a whole."""
    tx = Transaction.from_hex(str(vector["tx"]))
    digest = zip244.signature_digest(
        tx, SigHashType(SigHashUnit.ALL), _prev_outputs(vector), index=None
    )
    assert digest.hex() == str(vector["sighash_shielded"])


@pytest.mark.parametrize("vector", ZIP244, ids=range(len(ZIP244)))
@pytest.mark.parametrize(
    ("column", "sighash"),
    SIGHASH_VARIANTS,
    ids=[c for c, _ in SIGHASH_VARIANTS],
)
def test_zip244_transparent_sighashes(
    vector: dict[str, object], column: str, sighash: SigHashType
) -> None:
    expected = vector[column]
    if expected is None:
        # SIGHASH_SINGLE with no output at this input's index: the vectors
        # record no digest, because the signature commits to no output.
        return

    index = vector["transparent_input"]
    assert isinstance(index, int)

    tx = Transaction.from_hex(str(vector["tx"]))
    digest = zip244.signature_digest(tx, sighash, _prev_outputs(vector), index)
    assert digest.hex() == str(expected)


# --- ZIP 143 (v3) and ZIP 243 (v4) ------------------------------------------


@pytest.mark.parametrize(
    "vector",
    [*ZIP143, *ZIP243],
    ids=[f"zip143_{i}" for i in range(len(ZIP143))]
    + [f"zip243_{i}" for i in range(len(ZIP243))],
)
def test_zip143_and_zip243_sighashes(vector: dict[str, object]) -> None:
    """The two are not the same digest, and the difference is easy to miss.

    ZIP 143 has no Sapling, so its digest has no shielded-spend, shielded-output
    or value-balance fields; ZIP 243 adds exactly those. The test framework used
    the ZIP 243 layout for both, so it would compute the wrong digest for a v3
    transaction. These vectors cover both.
    """
    tx = Transaction.from_hex(str(vector["tx"]))
    sighash = SigHashType.from_byte(int(str(vector["hash_type"])))
    branch_id = int(str(vector["consensus_branch_id"]))

    index = int(str(vector["transparent_input"]))
    txin = (
        None
        if index < 0  # -1 means the signature is for a shielded spend
        else SigningInput(
            index=index,
            script_code=Script(bytes.fromhex(str(vector["script_code"]))),
            value=Zatoshi(int(str(vector["amount"]))),
        )
    )

    digest = zip143.signature_digest(tx, sighash, branch_id, txin)
    assert digest.hex() == str(vector["sighash"])


def test_the_v3_and_v4_corpora_are_not_empty() -> None:
    """These files are the only check on the pre-NU5 sighashes."""
    assert len(ZIP143) > 1
    assert len(ZIP243) > 1


# --- sighash types ----------------------------------------------------------


def test_sighash_bytes_round_trip() -> None:
    for unit in SigHashUnit:
        for anyone in (False, True):
            sighash = SigHashType(unit, anyone_can_pay=anyone)
            assert SigHashType.from_byte(sighash.value) == sighash


def test_an_unknown_sighash_base_type_is_refused() -> None:
    """Bitcoin silently treats an unknown base type as SIGHASH_ALL.

    That means a byte asking for something unrecognized produces a signature
    over every output. This library refuses instead of signing something broader
    than was asked for.
    """
    with pytest.raises(ParseError, match="unknown sighash base type"):
        SigHashType.from_byte(0x00)
    with pytest.raises(ParseError, match="unknown sighash base type"):
        SigHashType.from_byte(0x04)


def test_sighash_str_is_readable() -> None:
    assert str(SigHashType(SigHashUnit.ALL)) == "SIGHASH_ALL"
    assert (
        str(SigHashType(SigHashUnit.SINGLE, anyone_can_pay=True))
        == "SIGHASH_SINGLE|ANYONECANPAY"
    )


# --- the wrong ZIP for the version ------------------------------------------


def test_zip244_refuses_a_pre_v5_transaction() -> None:
    tx = Transaction.from_hex(str(ZIP243[0]["tx"]))
    with pytest.raises(ParseError, match="v5 transactions"):
        txid_digest(tx)


def test_zip143_refuses_a_v5_transaction() -> None:
    tx = Transaction.from_hex(str(ZIP244[0]["tx"]))
    with pytest.raises(ParseError, match="uses ZIP 244"):
        zip143.signature_digest(tx, SigHashType(), branch_id=0)


def test_signing_requires_the_previous_output_of_every_input() -> None:
    """ZIP 244 commits to the value and script of every transparent input.

    Not just the one being signed. That is what lets a signer see the whole
    transparent side of what it is signing, and therefore the true fee, without
    trusting whoever handed it the transaction. Omitting them is refused rather
    than silently digesting an empty list.
    """
    vector = next(v for v in ZIP244 if v["transparent_input"] is not None)
    tx = Transaction.from_hex(str(vector["tx"]))
    with pytest.raises(ParseError, match="previous output of every"):
        zip244.signature_digest(tx, SigHashType(), [], index=0)


def test_a_shielded_signature_must_use_sighash_all() -> None:
    """There is no transparent input for the other types to be relative to."""
    vector = next(v for v in ZIP244 if v["transparent_input"] is not None)
    tx = Transaction.from_hex(str(vector["tx"]))
    with pytest.raises(ParseError, match="must use SIGHASH_ALL"):
        zip244.signature_digest(
            tx,
            SigHashType(SigHashUnit.NONE),
            _prev_outputs(vector),
            index=None,
        )


def test_a_coinbase_transaction_needs_no_previous_outputs() -> None:
    """A coinbase input spends nothing, so there is no previous output at all.

    Its signature digest falls back to the plain transparent digest, which
    commits to no amounts and no scripts.
    """
    coinbase = [
        v for v in ZIP244 if Transaction.from_hex(str(v["tx"])).is_coinbase
    ]
    assert coinbase, "no coinbase vector to cover this path"
    for vector in coinbase:
        tx = Transaction.from_hex(str(vector["tx"]))
        assert not _prev_outputs(vector)
        digest = zip244.signature_digest(
            tx, SigHashType(SigHashUnit.ALL), [], index=None
        )
        assert digest.hex() == str(vector["sighash_shielded"])
