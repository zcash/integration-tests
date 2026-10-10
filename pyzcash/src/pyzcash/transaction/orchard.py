"""The Orchard bundle (ZIP 225).

Orchard has no separate spends and outputs: it has *actions*, each of which is
one spend and one output at once. That is what hides how much a transaction is
actually doing, since a dummy spend and a real one are indistinguishable.

Like Sapling in v5, the effecting data comes first and the authorizing data (the
proof and the signatures) is grouped at the end.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from pyzcash.consensus import ZERO, Zatoshi
from pyzcash.errors import ParseError

if TYPE_CHECKING:
    from pyzcash.encoding import Reader, Writer

__all__ = [
    "ENC_CIPHERTEXT_LEN",
    "OUT_CIPHERTEXT_LEN",
    "REDPALLAS_SIG_LEN",
    "OrchardAction",
    "OrchardBundle",
    "OrchardFlags",
]

ENC_CIPHERTEXT_LEN = 580
OUT_CIPHERTEXT_LEN = 80
REDPALLAS_SIG_LEN = 64

_FLAG_ENABLE_SPENDS = 0b0000_0001
_FLAG_ENABLE_OUTPUTS = 0b0000_0010


@dataclass(frozen=True, slots=True)
class OrchardFlags:
    """The bundle flags byte.

    The reserved bits are kept exactly as they were read. They must be zero
    under the current consensus rules, but this library reports what a
    transaction says rather than what it ought to say, and normalizing them away
    would change the bytes and therefore the txid.
    """

    enable_spends: bool
    enable_outputs: bool
    reserved: int = 0

    @classmethod
    def from_byte(cls, value: int) -> Self:
        return cls(
            enable_spends=bool(value & _FLAG_ENABLE_SPENDS),
            enable_outputs=bool(value & _FLAG_ENABLE_OUTPUTS),
            reserved=value & ~(_FLAG_ENABLE_SPENDS | _FLAG_ENABLE_OUTPUTS),
        )

    def to_byte(self) -> int:
        value = self.reserved
        if self.enable_spends:
            value |= _FLAG_ENABLE_SPENDS
        if self.enable_outputs:
            value |= _FLAG_ENABLE_OUTPUTS
        return value


@dataclass(frozen=True, slots=True)
class OrchardAction:
    """One Orchard action: a spend and an output, inseparably."""

    cv: bytes
    nullifier: bytes
    rk: bytes
    cmx: bytes
    ephemeral_key: bytes
    enc_ciphertext: bytes
    out_ciphertext: bytes
    spend_auth_sig: bytes

    def __post_init__(self) -> None:
        for name, value, expected in (
            ("cv", self.cv, 32),
            ("nullifier", self.nullifier, 32),
            ("rk", self.rk, 32),
            ("cmx", self.cmx, 32),
            ("ephemeral key", self.ephemeral_key, 32),
            ("enc ciphertext", self.enc_ciphertext, ENC_CIPHERTEXT_LEN),
            ("out ciphertext", self.out_ciphertext, OUT_CIPHERTEXT_LEN),
            ("spend auth signature", self.spend_auth_sig, REDPALLAS_SIG_LEN),
        ):
            if len(value) != expected:
                raise ParseError(
                    f"an Orchard {name} is {expected} bytes, got {len(value)}"
                )


@dataclass(frozen=True, slots=True)
class OrchardBundle:
    """An Orchard bundle. An empty one serializes to a single zero byte."""

    actions: tuple[OrchardAction, ...] = ()
    flags: OrchardFlags | None = None
    value_balance: Zatoshi = ZERO
    anchor: bytes | None = None
    proof: bytes = b""
    binding_sig: bytes | None = None

    @property
    def is_empty(self) -> bool:
        return not self.actions

    @classmethod
    def read(cls, reader: Reader) -> Self:
        count = reader.read_compact_size()
        if count == 0:
            return cls()

        actions_raw = [
            (
                reader.read_u256(),  # cv
                reader.read_u256(),  # nullifier
                reader.read_u256(),  # rk
                reader.read_u256(),  # cmx
                reader.read_u256(),  # ephemeral key
                reader.read(ENC_CIPHERTEXT_LEN),
                reader.read(OUT_CIPHERTEXT_LEN),
            )
            for _ in range(count)
        ]

        flags = OrchardFlags.from_byte(reader.read_u8())
        value_balance = Zatoshi(reader.read_i64())
        anchor = reader.read_u256()
        proof = reader.read_bytes_compact()
        sigs = [reader.read(REDPALLAS_SIG_LEN) for _ in range(count)]
        binding_sig = reader.read(REDPALLAS_SIG_LEN)

        actions = tuple(
            OrchardAction(
                cv=cv,
                nullifier=nullifier,
                rk=rk,
                cmx=cmx,
                ephemeral_key=epk,
                enc_ciphertext=enc,
                out_ciphertext=out,
                spend_auth_sig=sig,
            )
            for (cv, nullifier, rk, cmx, epk, enc, out), sig in zip(
                actions_raw, sigs, strict=True
            )
        )
        return cls(
            actions=actions,
            flags=flags,
            value_balance=value_balance,
            anchor=anchor,
            proof=proof,
            binding_sig=binding_sig,
        )

    def write(self, writer: Writer) -> None:
        writer.write_compact_size(len(self.actions))
        if not self.actions:
            return

        for action in self.actions:
            writer.write_u256(action.cv)
            writer.write_u256(action.nullifier)
            writer.write_u256(action.rk)
            writer.write_u256(action.cmx)
            writer.write_u256(action.ephemeral_key)
            writer.write(action.enc_ciphertext)
            writer.write(action.out_ciphertext)

        if (
            self.flags is None
            or self.anchor is None
            or self.binding_sig is None
        ):
            raise ParseError(
                "a non-empty Orchard bundle needs flags, an anchor, and a "
                "binding signature"
            )
        writer.write_u8(self.flags.to_byte())
        writer.write_i64(self.value_balance.value)
        writer.write_u256(self.anchor)
        writer.write_bytes_compact(self.proof)
        for action in self.actions:
            writer.write(action.spend_auth_sig)
        writer.write(self.binding_sig)
