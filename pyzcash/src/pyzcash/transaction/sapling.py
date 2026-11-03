"""The Sapling bundle.

Sapling is serialized two different ways, and the difference is the whole reason
ZIP 225 exists.

In a v4 transaction each spend and output is self-contained: the spend carries
its own anchor, proof, and signature inline. In a v5 transaction the
descriptions are stripped to their *effecting* data (what the transaction does)
and the *authorizing* data (the proofs and signatures, which prove it was
allowed) is grouped and moved to the end. That split is what lets ZIP 244
compute a txid over the effects alone, so that a transaction's identity stops
changing when its signatures do.

One bundle type models both, and the transaction version picks the layout.
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
    "PROOF_LEN",
    "REDJUBJUB_SIG_LEN",
    "SaplingBundle",
    "SaplingOutput",
    "SaplingSpend",
]

PROOF_LEN = 192
REDJUBJUB_SIG_LEN = 64
ENC_CIPHERTEXT_LEN = 580
OUT_CIPHERTEXT_LEN = 80


@dataclass(frozen=True, slots=True)
class SaplingSpend:
    """One Sapling spend.

    ``anchor`` is per-spend in a v4 transaction and None in a v5 one, where a
    single anchor is held by the bundle. Both are kept so a bundle can be
    re-serialized into the version it came from, byte for byte.
    """

    cv: bytes
    nullifier: bytes
    rk: bytes
    proof: bytes
    spend_auth_sig: bytes
    anchor: bytes | None = None

    def __post_init__(self) -> None:
        _check(self.cv, 32, "cv")
        _check(self.nullifier, 32, "nullifier")
        _check(self.rk, 32, "rk")
        _check(self.proof, PROOF_LEN, "spend proof")
        _check(self.spend_auth_sig, REDJUBJUB_SIG_LEN, "spend auth signature")
        if self.anchor is not None:
            _check(self.anchor, 32, "anchor")


@dataclass(frozen=True, slots=True)
class SaplingOutput:
    """One Sapling output."""

    cv: bytes
    cmu: bytes
    ephemeral_key: bytes
    enc_ciphertext: bytes
    out_ciphertext: bytes
    proof: bytes

    def __post_init__(self) -> None:
        _check(self.cv, 32, "cv")
        _check(self.cmu, 32, "cmu")
        _check(self.ephemeral_key, 32, "ephemeral key")
        _check(self.enc_ciphertext, ENC_CIPHERTEXT_LEN, "enc ciphertext")
        _check(self.out_ciphertext, OUT_CIPHERTEXT_LEN, "out ciphertext")
        _check(self.proof, PROOF_LEN, "output proof")


@dataclass(frozen=True, slots=True)
class SaplingBundle:
    """A Sapling bundle: its spends, its outputs, and what balances them."""

    spends: tuple[SaplingSpend, ...] = ()
    outputs: tuple[SaplingOutput, ...] = ()
    value_balance: Zatoshi = ZERO
    anchor: bytes | None = None
    binding_sig: bytes | None = None

    @property
    def is_empty(self) -> bool:
        return not self.spends and not self.outputs

    # --- v5 (ZIP 225): effects first, authorizing data grouped at the end

    @classmethod
    def read_v5(cls, reader: Reader) -> Self:
        spend_count = reader.read_compact_size()
        spends_raw = [
            (reader.read_u256(), reader.read_u256(), reader.read_u256())
            for _ in range(spend_count)
        ]
        output_count = reader.read_compact_size()
        outputs_raw = [
            (
                reader.read_u256(),
                reader.read_u256(),
                reader.read_u256(),
                reader.read(ENC_CIPHERTEXT_LEN),
                reader.read(OUT_CIPHERTEXT_LEN),
            )
            for _ in range(output_count)
        ]

        has_sapling = bool(spend_count or output_count)
        value_balance = (
            Zatoshi(reader.read_i64()) if has_sapling else Zatoshi(0)
        )
        anchor = reader.read_u256() if spend_count else None

        spend_proofs = [reader.read(PROOF_LEN) for _ in range(spend_count)]
        spend_sigs = [
            reader.read(REDJUBJUB_SIG_LEN) for _ in range(spend_count)
        ]
        output_proofs = [reader.read(PROOF_LEN) for _ in range(output_count)]
        binding_sig = reader.read(REDJUBJUB_SIG_LEN) if has_sapling else None

        spends = tuple(
            SaplingSpend(
                cv=cv,
                nullifier=nullifier,
                rk=rk,
                proof=proof,
                spend_auth_sig=sig,
                anchor=None,
            )
            for (cv, nullifier, rk), proof, sig in zip(
                spends_raw, spend_proofs, spend_sigs, strict=True
            )
        )
        outputs = tuple(
            SaplingOutput(
                cv=cv,
                cmu=cmu,
                ephemeral_key=epk,
                enc_ciphertext=enc,
                out_ciphertext=out,
                proof=proof,
            )
            for (cv, cmu, epk, enc, out), proof in zip(
                outputs_raw, output_proofs, strict=True
            )
        )
        return cls(
            spends=spends,
            outputs=outputs,
            value_balance=value_balance,
            anchor=anchor,
            binding_sig=binding_sig,
        )

    def write_v5(self, writer: Writer) -> None:
        writer.write_compact_size(len(self.spends))
        for spend in self.spends:
            writer.write_u256(spend.cv)
            writer.write_u256(spend.nullifier)
            writer.write_u256(spend.rk)

        writer.write_compact_size(len(self.outputs))
        for output in self.outputs:
            writer.write_u256(output.cv)
            writer.write_u256(output.cmu)
            writer.write_u256(output.ephemeral_key)
            writer.write(output.enc_ciphertext)
            writer.write(output.out_ciphertext)

        if self.is_empty:
            return

        writer.write_i64(self.value_balance.value)
        if self.spends:
            if self.anchor is None:
                raise ParseError(
                    "a v5 bundle with spends needs a bundle anchor"
                )
            writer.write_u256(self.anchor)

        for spend in self.spends:
            writer.write(spend.proof)
        for spend in self.spends:
            writer.write(spend.spend_auth_sig)
        for output in self.outputs:
            writer.write(output.proof)

        if self.binding_sig is None:
            raise ParseError("a non-empty Sapling bundle needs a binding sig")
        writer.write(self.binding_sig)

    # --- v4: each description is self-contained ------------------------

    @staticmethod
    def read_v4_spend(reader: Reader) -> SaplingSpend:
        return SaplingSpend(
            cv=reader.read_u256(),
            anchor=reader.read_u256(),
            nullifier=reader.read_u256(),
            rk=reader.read_u256(),
            proof=reader.read(PROOF_LEN),
            spend_auth_sig=reader.read(REDJUBJUB_SIG_LEN),
        )

    @staticmethod
    def write_v4_spend(writer: Writer, spend: SaplingSpend) -> None:
        if spend.anchor is None:
            raise ParseError("a v4 spend carries its own anchor")
        writer.write_u256(spend.cv)
        writer.write_u256(spend.anchor)
        writer.write_u256(spend.nullifier)
        writer.write_u256(spend.rk)
        writer.write(spend.proof)
        writer.write(spend.spend_auth_sig)

    @staticmethod
    def read_v4_output(reader: Reader) -> SaplingOutput:
        return SaplingOutput(
            cv=reader.read_u256(),
            cmu=reader.read_u256(),
            ephemeral_key=reader.read_u256(),
            enc_ciphertext=reader.read(ENC_CIPHERTEXT_LEN),
            out_ciphertext=reader.read(OUT_CIPHERTEXT_LEN),
            proof=reader.read(PROOF_LEN),
        )

    @staticmethod
    def write_v4_output(writer: Writer, output: SaplingOutput) -> None:
        writer.write_u256(output.cv)
        writer.write_u256(output.cmu)
        writer.write_u256(output.ephemeral_key)
        writer.write(output.enc_ciphertext)
        writer.write(output.out_ciphertext)
        writer.write(output.proof)


def _check(value: bytes, expected: int, name: str) -> None:
    if len(value) != expected:
        raise ParseError(
            f"a Sapling {name} is {expected} bytes, got {len(value)}"
        )
