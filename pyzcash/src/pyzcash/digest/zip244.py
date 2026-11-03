"""ZIP 244: the txid, the authorizing-data commitment, and the v5 sighash.

Before ZIP 244, a transaction's txid was a hash of its serialized bytes, so it
changed whenever a signature changed. ZIP 244 splits a transaction into what it
*does* (its effects) and what *authorizes* it (proofs and signatures), and makes
the txid a digest of the effects alone. The identity of a transaction therefore
no longer depends on how it was signed, which is what makes it safe to sign
something that refers to a txid.

The result is a tree of BLAKE2b digests, each with its own personalization
string. The personalizations are the whole security argument: two digests over
identical bytes still differ if their personalizations do, so a value from one
part of the tree can never be substituted into another.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pyzcash.digest.sighash_type import SigHashType, SigHashUnit
from pyzcash.encoding import Writer, blake2b_personal
from pyzcash.errors import ParseError
from pyzcash.transaction import (
    OrchardBundle,
    SaplingBundle,
    Transaction,
    TxVersion,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pyzcash.consensus import Zatoshi
    from pyzcash.script import Script
    from pyzcash.transaction import TxOut


__all__ = [
    "PrevOutput",
    "auth_digest",
    "signature_digest",
    "txid_digest",
]

_EMPTY = b""

# Compact note ciphertexts are split three ways, so that a light client can hash
# the part it needs without the memo. These offsets are that split.
_COMPACT_END = 52
_MEMO_END = 564


@dataclass(frozen=True, slots=True)
class PrevOutput:
    """A previous output that a transparent input spends.

    A transparent signature commits to the value being spent and to the script
    being satisfied, and neither is in the transaction: both live in the
    *previous* output. Bitcoin's original sighash omitted the value, which is
    what allowed a hardware wallet to be induced to sign away a far larger fee
    than it had been shown. Zcash commits to it, and since ZIP 244 commits to
    the value and script of *every* transparent input, not merely the one being
    signed, so a signer can be sure of the whole transparent side of what it is
    signing.

    That is why signing needs this: the transaction alone is not enough.
    """

    value: Zatoshi
    script_pubkey: Script


# --- transparent ------------------------------------------------------------


def _prevouts_digest(tx: Transaction, personal: bytes) -> bytes:
    writer = Writer()
    for txin in tx.vin:
        txin.prevout.write(writer)
    return blake2b_personal(personal, writer.to_bytes())


def _sequence_digest(tx: Transaction, personal: bytes) -> bytes:
    writer = Writer()
    for txin in tx.vin:
        writer.write_u32(txin.sequence)
    return blake2b_personal(personal, writer.to_bytes())


def _outputs_digest(outputs: tuple[TxOut, ...], personal: bytes) -> bytes:
    writer = Writer()
    for txout in outputs:
        txout.write(writer)
    return blake2b_personal(personal, writer.to_bytes())


def transparent_digest(tx: Transaction) -> bytes:
    """The effects of the transparent bundle: what it spends, what it pays."""
    if not tx.vin and not tx.vout:
        return blake2b_personal(b"ZTxIdTranspaHash", _EMPTY)
    body = (
        _prevouts_digest(tx, b"ZTxIdPrevoutHash")
        + _sequence_digest(tx, b"ZTxIdSequencHash")
        + _outputs_digest(tx.vout, b"ZTxIdOutputsHash")
    )
    return blake2b_personal(b"ZTxIdTranspaHash", body)


def transparent_auth_digest(tx: Transaction) -> bytes:
    """The authorizing data of the transparent bundle: the input scripts."""
    writer = Writer()
    for txin in tx.vin:
        writer.write_bytes_compact(txin.script_sig.raw)
    return blake2b_personal(b"ZTxAuthTransHash", writer.to_bytes())


# --- Sapling ----------------------------------------------------------------


def sapling_digest(bundle: SaplingBundle) -> bytes:
    """The effects of the Sapling bundle."""
    if bundle.is_empty:
        return blake2b_personal(b"ZTxIdSaplingHash", _EMPTY)

    writer = Writer()
    writer.write(_sapling_spends_digest(bundle))
    writer.write(_sapling_outputs_digest(bundle))
    writer.write_i64(bundle.value_balance.value)
    return blake2b_personal(b"ZTxIdSaplingHash", writer.to_bytes())


def sapling_auth_digest(bundle: SaplingBundle) -> bytes:
    """The authorizing data of the Sapling bundle: its proofs and signatures."""
    if bundle.is_empty:
        return blake2b_personal(b"ZTxAuthSapliHash", _EMPTY)

    writer = Writer()
    for spend in bundle.spends:
        writer.write(spend.proof)
    for spend in bundle.spends:
        writer.write(spend.spend_auth_sig)
    for output in bundle.outputs:
        writer.write(output.proof)
    if bundle.binding_sig is None:
        raise ParseError("a non-empty Sapling bundle needs a binding signature")
    writer.write(bundle.binding_sig)
    return blake2b_personal(b"ZTxAuthSapliHash", writer.to_bytes())


def _sapling_spends_digest(bundle: SaplingBundle) -> bytes:
    if not bundle.spends:
        return blake2b_personal(b"ZTxIdSSpendsHash", _EMPTY)

    compact = Writer()
    noncompact = Writer()
    for spend in bundle.spends:
        compact.write_u256(spend.nullifier)

        noncompact.write_u256(spend.cv)
        anchor = bundle.anchor if bundle.anchor is not None else spend.anchor
        if anchor is None:
            raise ParseError("a Sapling spend has no anchor to commit to")
        noncompact.write_u256(anchor)
        noncompact.write_u256(spend.rk)

    body = blake2b_personal(
        b"ZTxIdSSpendCHash", compact.to_bytes()
    ) + blake2b_personal(b"ZTxIdSSpendNHash", noncompact.to_bytes())
    return blake2b_personal(b"ZTxIdSSpendsHash", body)


def _sapling_outputs_digest(bundle: SaplingBundle) -> bytes:
    if not bundle.outputs:
        return blake2b_personal(b"ZTxIdSOutputHash", _EMPTY)

    compact = Writer()
    memos = Writer()
    noncompact = Writer()
    for output in bundle.outputs:
        compact.write_u256(output.cmu)
        compact.write_u256(output.ephemeral_key)
        compact.write(output.enc_ciphertext[:_COMPACT_END])

        memos.write(output.enc_ciphertext[_COMPACT_END:_MEMO_END])

        noncompact.write_u256(output.cv)
        noncompact.write(output.enc_ciphertext[_MEMO_END:])
        noncompact.write(output.out_ciphertext)

    body = (
        blake2b_personal(b"ZTxIdSOutC__Hash", compact.to_bytes())
        + blake2b_personal(b"ZTxIdSOutM__Hash", memos.to_bytes())
        + blake2b_personal(b"ZTxIdSOutN__Hash", noncompact.to_bytes())
    )
    return blake2b_personal(b"ZTxIdSOutputHash", body)


# --- Orchard ----------------------------------------------------------------


def orchard_digest(bundle: OrchardBundle) -> bytes:
    """The effects of the Orchard bundle."""
    if bundle.is_empty:
        return blake2b_personal(b"ZTxIdOrchardHash", _EMPTY)
    if bundle.flags is None or bundle.anchor is None:
        raise ParseError("a non-empty Orchard bundle needs flags and an anchor")

    compact = Writer()
    memos = Writer()
    noncompact = Writer()
    for action in bundle.actions:
        compact.write_u256(action.nullifier)
        compact.write_u256(action.cmx)
        compact.write_u256(action.ephemeral_key)
        compact.write(action.enc_ciphertext[:_COMPACT_END])

        memos.write(action.enc_ciphertext[_COMPACT_END:_MEMO_END])

        noncompact.write_u256(action.cv)
        noncompact.write_u256(action.rk)
        noncompact.write(action.enc_ciphertext[_MEMO_END:])
        noncompact.write(action.out_ciphertext)

    writer = Writer()
    writer.write(blake2b_personal(b"ZTxIdOrcActCHash", compact.to_bytes()))
    writer.write(blake2b_personal(b"ZTxIdOrcActMHash", memos.to_bytes()))
    writer.write(blake2b_personal(b"ZTxIdOrcActNHash", noncompact.to_bytes()))
    writer.write_u8(bundle.flags.to_byte())
    writer.write_i64(bundle.value_balance.value)
    writer.write_u256(bundle.anchor)
    return blake2b_personal(b"ZTxIdOrchardHash", writer.to_bytes())


def orchard_auth_digest(bundle: OrchardBundle) -> bytes:
    """The authorizing data of the Orchard bundle: its proof and signatures."""
    if bundle.is_empty:
        return blake2b_personal(b"ZTxAuthOrchaHash", _EMPTY)
    if bundle.binding_sig is None:
        raise ParseError("a non-empty Orchard bundle needs a binding signature")

    writer = Writer()
    writer.write(bundle.proof)
    for action in bundle.actions:
        writer.write(action.spend_auth_sig)
    writer.write(bundle.binding_sig)
    return blake2b_personal(b"ZTxAuthOrchaHash", writer.to_bytes())


# --- the transaction --------------------------------------------------------


def _header_digest(tx: Transaction) -> bytes:
    if tx.consensus_branch_id is None or tx.expiry_height is None:
        raise ParseError("a v5 transaction needs a branch ID and expiry height")
    writer = Writer()
    writer.write_u32((1 << 31) | tx.version.number)
    writer.write_u32(tx.version.version_group_id)
    writer.write_u32(tx.consensus_branch_id)
    writer.write_u32(tx.lock_time)
    writer.write_u32(tx.expiry_height)
    return blake2b_personal(b"ZTxIdHeadersHash", writer.to_bytes())


def _require_v5(tx: Transaction) -> int:
    if tx.version is not TxVersion.V5:
        raise ParseError(
            f"ZIP 244 digests are defined for v5 transactions; this is "
            f"{tx.version.name}"
        )
    if tx.consensus_branch_id is None:
        raise ParseError("a v5 transaction needs a consensus branch ID")
    return tx.consensus_branch_id


def txid_digest(tx: Transaction) -> bytes:
    """The txid of a v5 transaction: a digest of what it does.

    Not a hash of its bytes. Re-signing the transaction does not change this.
    """
    branch_id = _require_v5(tx)
    writer = Writer()
    writer.write(_header_digest(tx))
    writer.write(transparent_digest(tx))
    writer.write(sapling_digest(tx.sapling_bundle))
    writer.write(orchard_digest(tx.orchard_bundle))
    return blake2b_personal(
        b"ZcashTxHash_" + branch_id.to_bytes(4, "little"), writer.to_bytes()
    )


def auth_digest(tx: Transaction) -> bytes:
    """The commitment to a transaction's authorizing data.

    The txid covers what the transaction does; this covers what proves it was
    allowed. A block commits to both, so the signatures are still bound to the
    chain even though they are outside the txid.
    """
    branch_id = _require_v5(tx)
    writer = Writer()
    writer.write(transparent_auth_digest(tx))
    writer.write(sapling_auth_digest(tx.sapling_bundle))
    writer.write(orchard_auth_digest(tx.orchard_bundle))
    return blake2b_personal(
        b"ZTxAuthHash_" + branch_id.to_bytes(4, "little"), writer.to_bytes()
    )


def signature_digest(
    tx: Transaction,
    sighash: SigHashType,
    prev_outputs: Sequence[PrevOutput],
    index: int | None = None,
) -> bytes:
    """The digest a v5 signature is made over.

    Args:
        tx: the transaction being signed.
        sighash: which parts of it the signature commits to. A shielded
            signature must use SIGHASH_ALL, since there is no transparent input
            for the other types to be relative to.
        prev_outputs: the previous output of *every* transparent input, in
            order. ZIP 244 commits to all of their values and scripts, not just
            the one being signed.
        index: which transparent input is being signed, or None when signing a
            shielded spend or an Orchard action.

    Raises:
        ParseError: if prev_outputs does not match the transaction's inputs, if
            the index is out of range, or if a shielded signature asks for a
            sighash type other than SIGHASH_ALL.
    """
    branch_id = _require_v5(tx)

    # A coinbase input spends nothing, so it has no previous output, and a
    # transaction with no transparent inputs needs none either. In both cases
    # the signature digest falls back to the plain transparent digest, which
    # commits to no amounts and no scripts, so none are required here.
    if not tx.is_coinbase and tx.vin and len(prev_outputs) != len(tx.vin):
        raise ParseError(
            f"signing needs the previous output of every transparent input: "
            f"the transaction has {len(tx.vin)}, but {len(prev_outputs)} "
            f"were given"
        )
    if index is None:
        if sighash != SigHashType(SigHashUnit.ALL):
            raise ParseError(
                f"a shielded signature commits to the whole transaction, so it "
                f"must use SIGHASH_ALL, not {sighash}"
            )
    elif not 0 <= index < len(tx.vin):
        raise ParseError(
            f"input index {index} is out of range for a transaction with "
            f"{len(tx.vin)} input(s)"
        )
    elif sighash.unit is SigHashUnit.SINGLE and index >= len(tx.vout):
        raise ParseError(
            f"SIGHASH_SINGLE signs the output at the input's index, but input "
            f"{index} has no matching output among {len(tx.vout)}"
        )

    writer = Writer()
    writer.write(_header_digest(tx))
    writer.write(_transparent_sig_digest(tx, sighash, prev_outputs, index))
    writer.write(sapling_digest(tx.sapling_bundle))
    writer.write(orchard_digest(tx.orchard_bundle))
    return blake2b_personal(
        b"ZcashTxHash_" + branch_id.to_bytes(4, "little"), writer.to_bytes()
    )


def _transparent_sig_digest(
    tx: Transaction,
    sighash: SigHashType,
    prev_outputs: Sequence[PrevOutput],
    index: int | None,
) -> bytes:
    # A coinbase transaction, or one with no transparent inputs, has nothing
    # input-specific to commit to, so its signature digest reuses the plain
    # transparent digest.
    if tx.is_coinbase or not tx.vin:
        return transparent_digest(tx)

    body = (
        bytes([sighash.value])
        + _prevouts_sig_digest(tx, sighash)
        + _amounts_sig_digest(prev_outputs, sighash)
        + _scriptpubkeys_sig_digest(prev_outputs, sighash)
        + _sequence_sig_digest(tx, sighash)
        + _outputs_sig_digest(tx, sighash, index)
        + _txin_sig_digest(tx, prev_outputs, index)
    )
    return blake2b_personal(b"ZTxIdTranspaHash", body)


def _prevouts_sig_digest(tx: Transaction, sighash: SigHashType) -> bytes:
    if sighash.anyone_can_pay:
        # The signature does not commit to which other inputs are spent, so
        # anyone may add one of their own.
        return blake2b_personal(b"ZTxIdPrevoutHash", _EMPTY)
    return _prevouts_digest(tx, b"ZTxIdPrevoutHash")


def _amounts_sig_digest(
    prev_outputs: Sequence[PrevOutput], sighash: SigHashType
) -> bytes:
    """Commits to the value of every input being spent.

    This is what lets a signer know the total it is spending, and therefore the
    fee, without trusting whoever handed it the transaction.
    """
    if sighash.anyone_can_pay:
        return blake2b_personal(b"ZTxTrAmountsHash", _EMPTY)
    writer = Writer()
    for prev in prev_outputs:
        writer.write_u64(prev.value.value)
    return blake2b_personal(b"ZTxTrAmountsHash", writer.to_bytes())


def _scriptpubkeys_sig_digest(
    prev_outputs: Sequence[PrevOutput], sighash: SigHashType
) -> bytes:
    """Commits to the script of every input being spent."""
    if sighash.anyone_can_pay:
        return blake2b_personal(b"ZTxTrScriptsHash", _EMPTY)
    writer = Writer()
    for prev in prev_outputs:
        writer.write_bytes_compact(prev.script_pubkey.raw)
    return blake2b_personal(b"ZTxTrScriptsHash", writer.to_bytes())


def _sequence_sig_digest(tx: Transaction, sighash: SigHashType) -> bytes:
    if sighash.anyone_can_pay:
        return blake2b_personal(b"ZTxIdSequencHash", _EMPTY)
    return _sequence_digest(tx, b"ZTxIdSequencHash")


def _outputs_sig_digest(
    tx: Transaction, sighash: SigHashType, index: int | None
) -> bytes:
    if sighash.unit is SigHashUnit.ALL:
        return _outputs_digest(tx.vout, b"ZTxIdOutputsHash")

    if (
        sighash.unit is SigHashUnit.SINGLE
        and index is not None
        and 0 <= index < len(tx.vout)
    ):
        # Commits to exactly the one output that pairs with this input.
        return _outputs_digest((tx.vout[index],), b"ZTxIdOutputsHash")

    # SIGHASH_NONE: the signature commits to no output at all, so anyone may
    # redirect the funds.
    return blake2b_personal(b"ZTxIdOutputsHash", _EMPTY)


def _txin_sig_digest(
    tx: Transaction, prev_outputs: Sequence[PrevOutput], index: int | None
) -> bytes:
    """The one input being signed. Empty for a shielded signature."""
    if index is None:
        return blake2b_personal(b"Zcash___TxInHash", _EMPTY)

    writer = Writer()
    tx.vin[index].prevout.write(writer)
    writer.write_u64(prev_outputs[index].value.value)
    writer.write_bytes_compact(prev_outputs[index].script_pubkey.raw)
    writer.write_u32(tx.vin[index].sequence)
    return blake2b_personal(b"Zcash___TxInHash", writer.to_bytes())
