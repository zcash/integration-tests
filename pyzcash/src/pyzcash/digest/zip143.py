"""ZIP 143 and ZIP 243: the sighash for v3 and v4 transactions.

These are the predecessors of ZIP 244. They are not the same as each other, and
that difference is easy to miss: ZIP 143 (Overwinter, v3) has no Sapling, so its
digest has no shielded-spend, shielded-output, or value-balance fields at all,
while ZIP 243 (Sapling, v4) adds exactly those three. The test framework this
code was extracted from used the ZIP 243 layout for every transaction, so it
would compute the wrong digest for a v3 one.

Both commit to the value of the output being spent, which Bitcoin's original
sighash did not. That omission is what let a wallet be induced to sign a
transaction whose fee was far larger than it had been shown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pyzcash.digest.sighash_type import SigHashType, SigHashUnit
from pyzcash.encoding import Writer, blake2b_personal
from pyzcash.errors import ParseError
from pyzcash.transaction import Transaction, TxVersion

if TYPE_CHECKING:
    from pyzcash.consensus import Zatoshi
    from pyzcash.script import Script

__all__ = ["SigningInput", "signature_digest"]


@dataclass(frozen=True, slots=True)
class SigningInput:
    """The transparent input being signed, and what it spends.

    ZIP 143 and ZIP 243 commit to a *script code* rather than to the previous
    output's scriptPubKey directly. For an ordinary P2PKH input the two are the
    same bytes, but for a P2SH input the script code is the redeem script. This
    is the field ZIP 244 later replaced with the scriptPubKey of every input.
    """

    index: int
    script_code: Script
    value: Zatoshi


_EMPTY = b""
_ZERO = bytes(32)


def signature_digest(
    tx: Transaction,
    sighash: SigHashType,
    branch_id: int,
    txin: SigningInput | None = None,
) -> bytes:
    """The digest a v3 or v4 signature is made over.

    Args:
        tx: the transaction being signed. Must be v3 (ZIP 143) or v4 (ZIP 243).
        sighash: which parts of it the signature commits to.
        branch_id: the consensus branch ID. Unlike v5, a pre-NU5 transaction
            does not carry this in its header, so the caller must supply it.
            It is what stops a signature being replayed across a network
            upgrade.
        txin: the transparent input being signed, with the previous output's
            script and value. None when signing a shielded spend.

    Raises:
        ParseError: if the transaction is not v3 or v4.
    """
    if tx.version not in (TxVersion.V3, TxVersion.V4):
        raise ParseError(
            f"ZIP 143 and ZIP 243 sighashes are for v3 and v4 transactions; "
            f"this is {tx.version.name}. A v5 transaction uses ZIP 244."
        )
    if tx.expiry_height is None:
        raise ParseError("an overwintered transaction needs an expiry height")

    is_sapling = tx.version is TxVersion.V4

    writer = Writer()
    writer.write_u32((1 << 31) | tx.version.number)
    writer.write_u32(tx.version.version_group_id)
    writer.write(_prevouts(tx, sighash))
    writer.write(_sequence(tx, sighash))
    writer.write(_outputs(tx, sighash, txin))
    writer.write(_joinsplits(tx))

    if is_sapling:
        # These three fields exist only in ZIP 243. A v3 transaction has no
        # Sapling bundle, and its digest does not reserve space for one.
        writer.write(_shielded_spends(tx))
        writer.write(_shielded_outputs(tx))

    writer.write_u32(tx.lock_time)
    writer.write_u32(tx.expiry_height)

    if is_sapling:
        # Signed, not unsigned: a bundle that draws value out of the shielded
        # pool has a negative balance. The framework packed this as an unsigned
        # 64-bit value, which cannot represent one.
        writer.write_i64(tx.sapling_bundle.value_balance.value)

    writer.write_u32(sighash.value)

    if txin is not None:
        if not 0 <= txin.index < len(tx.vin):
            raise ParseError(
                f"input index {txin.index} is out of range for a transaction "
                f"with {len(tx.vin)} input(s)"
            )
        tx.vin[txin.index].prevout.write(writer)
        writer.write_bytes_compact(txin.script_code.raw)
        writer.write_u64(txin.value.value)
        writer.write_u32(tx.vin[txin.index].sequence)

    return blake2b_personal(
        b"ZcashSigHash" + branch_id.to_bytes(4, "little"), writer.to_bytes()
    )


def _prevouts(tx: Transaction, sighash: SigHashType) -> bytes:
    if sighash.anyone_can_pay:
        return _ZERO
    writer = Writer()
    for txin in tx.vin:
        txin.prevout.write(writer)
    return blake2b_personal(b"ZcashPrevoutHash", writer.to_bytes())


def _sequence(tx: Transaction, sighash: SigHashType) -> bytes:
    if sighash.anyone_can_pay or sighash.unit in (
        SigHashUnit.SINGLE,
        SigHashUnit.NONE,
    ):
        return _ZERO
    writer = Writer()
    for txin in tx.vin:
        writer.write_u32(txin.sequence)
    return blake2b_personal(b"ZcashSequencHash", writer.to_bytes())


def _outputs(
    tx: Transaction, sighash: SigHashType, txin: SigningInput | None
) -> bytes:
    if sighash.unit is SigHashUnit.ALL:
        writer = Writer()
        for txout in tx.vout:
            txout.write(writer)
        return blake2b_personal(b"ZcashOutputsHash", writer.to_bytes())

    if (
        sighash.unit is SigHashUnit.SINGLE
        and txin is not None
        and 0 <= txin.index < len(tx.vout)
    ):
        writer = Writer()
        tx.vout[txin.index].write(writer)
        return blake2b_personal(b"ZcashOutputsHash", writer.to_bytes())

    return _ZERO


def _joinsplits(tx: Transaction) -> bytes:
    if not tx.joinsplits:
        return _ZERO
    if tx.joinsplit_pubkey is None:
        raise ParseError("a transaction with JoinSplits needs a JoinSplit key")
    writer = Writer()
    for joinsplit in tx.joinsplits:
        joinsplit.write(writer)
    writer.write(tx.joinsplit_pubkey)
    return blake2b_personal(b"ZcashJSplitsHash", writer.to_bytes())


def _shielded_spends(tx: Transaction) -> bytes:
    spends = tx.sapling_bundle.spends
    if not spends:
        return _ZERO
    writer = Writer()
    for spend in spends:
        # The spend authorization signature is deliberately excluded: a
        # signature cannot commit to itself.
        writer.write_u256(spend.cv)
        if spend.anchor is None:
            raise ParseError("a v4 Sapling spend carries its own anchor")
        writer.write_u256(spend.anchor)
        writer.write_u256(spend.nullifier)
        writer.write_u256(spend.rk)
        writer.write(spend.proof)
    return blake2b_personal(b"ZcashSSpendsHash", writer.to_bytes())


def _shielded_outputs(tx: Transaction) -> bytes:
    outputs = tx.sapling_bundle.outputs
    if not outputs:
        return _ZERO
    writer = Writer()
    for output in outputs:
        writer.write_u256(output.cv)
        writer.write_u256(output.cmu)
        writer.write_u256(output.ephemeral_key)
        writer.write(output.enc_ciphertext)
        writer.write(output.out_ciphertext)
        writer.write(output.proof)
    return blake2b_personal(b"ZcashSOutputHash", writer.to_bytes())
