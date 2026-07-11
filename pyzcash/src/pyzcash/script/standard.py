"""The standard script templates, and the bridge between scripts and addresses.

A transparent address is not stored in a transaction: what is stored is the
script that pays to it. These functions are the translation, and they are the
reason the address layer exists at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyzcash.address import (
    HASH_LEN,
    TexAddress,
    TransparentAddress,
    TransparentKind,
)
from pyzcash.errors import ParseError
from pyzcash.script.opcodes import Opcode
from pyzcash.script.script import Operation, Script

if TYPE_CHECKING:
    from pyzcash.consensus import Network

__all__ = [
    "MAX_MULTISIG_KEYS",
    "address_from_script_pubkey",
    "multisig_script",
    "op_return_script",
    "p2pkh_script_pubkey",
    "p2sh_script_pubkey",
    "script_pubkey_for",
]

MAX_MULTISIG_KEYS = 16
"""OP_1 through OP_16 are the only ways to write the key count."""


def p2pkh_script_pubkey(pubkey_hash: bytes) -> Script:
    """OP_DUP OP_HASH160 <hash> OP_EQUALVERIFY OP_CHECKSIG."""
    _check_hash(pubkey_hash)
    return Script.build(
        Opcode.OP_DUP,
        Opcode.OP_HASH160,
        pubkey_hash,
        Opcode.OP_EQUALVERIFY,
        Opcode.OP_CHECKSIG,
    )


def p2sh_script_pubkey(script_hash: bytes) -> Script:
    """OP_HASH160 <hash> OP_EQUAL."""
    _check_hash(script_hash)
    return Script.build(Opcode.OP_HASH160, script_hash, Opcode.OP_EQUAL)


def op_return_script(data: bytes = b"") -> Script:
    """An unspendable output that carries data."""
    if data:
        return Script.build(Opcode.OP_RETURN, data)
    return Script.build(Opcode.OP_RETURN)


def multisig_script(required: int, pubkeys: list[bytes]) -> Script:
    """OP_m <pubkey>... OP_n OP_CHECKMULTISIG."""
    if not 1 <= required <= len(pubkeys) <= MAX_MULTISIG_KEYS:
        raise ValueError(
            f"a {required}-of-{len(pubkeys)} multisig is not expressible; "
            f"need 1 <= m <= n <= {MAX_MULTISIG_KEYS}"
        )
    return Script.build(
        Opcode.for_small_int(required),
        *pubkeys,
        Opcode.for_small_int(len(pubkeys)),
        Opcode.OP_CHECKMULTISIG,
    )


def script_pubkey_for(address: TransparentAddress | TexAddress) -> Script:
    """The script that pays to ``address``.

    A TEX address pays to the same P2PKH script as the transparent address it
    encodes: the distinction is about the sender's intent, not the script.

    Shielded addresses have no script. Funds reach them through a shielded
    bundle, not a transparent output, so passing one here is a type error rather
    than something to return None for.
    """
    if isinstance(address, TexAddress):
        return p2pkh_script_pubkey(address.hash)
    if address.kind is TransparentKind.P2PKH:
        return p2pkh_script_pubkey(address.hash)
    return p2sh_script_pubkey(address.hash)


def address_from_script_pubkey(
    script: Script, network: Network
) -> TransparentAddress | None:
    """The address a script pays to, or None if it is not a standard template.

    None means "this output does not pay to an address" (an OP_RETURN, a bare
    multisig, anything non-standard), which is a real and common thing for an
    output to be. It does not mean the script is invalid.

    A script whose bytes do not even parse also pays to no address, and so also
    returns None rather than raising. Such scripts are on the chain: consensus
    permits an output script with a truncated push, it is simply unspendable.
    Raising here would mean a caller walking the chain and asking each output
    "who does this pay?" would crash on one, which is not a question that has an
    exceptional answer.
    """
    try:
        ops = script.operations()
    except ParseError:
        return None

    match ops:
        case [
            Operation(opcode=Opcode.OP_DUP),
            Operation(opcode=Opcode.OP_HASH160),
            Operation(data=bytes() as h),
            Operation(opcode=Opcode.OP_EQUALVERIFY),
            Operation(opcode=Opcode.OP_CHECKSIG),
        ] if len(h) == HASH_LEN:
            return TransparentAddress(
                network=network, kind=TransparentKind.P2PKH, hash=h
            )
        case [
            Operation(opcode=Opcode.OP_HASH160),
            Operation(data=bytes() as h),
            Operation(opcode=Opcode.OP_EQUAL),
        ] if len(h) == HASH_LEN:
            return TransparentAddress(
                network=network, kind=TransparentKind.P2SH, hash=h
            )
        case _:
            return None


def _check_hash(value: bytes) -> None:
    if len(value) != HASH_LEN:
        raise ParseError(f"a script hash is {HASH_LEN} bytes, got {len(value)}")
