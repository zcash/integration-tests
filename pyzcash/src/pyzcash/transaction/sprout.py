"""Sprout JoinSplits.

Sprout is the original shielded pool, deprecated and no longer receivable, but
its transactions are on the chain forever, so anything that claims to parse
Zcash has to read them.

A JoinSplit carries one of two proof systems, and which one is not recorded in
the JoinSplit: it follows from the transaction version. Pre-Sapling (v2 and v3)
transactions carry PHGR13 proofs; Sapling-era (v4) transactions carry Groth16.
The test framework's parser defaulted to Groth16 for every version, and its
PHGR13 path could not run at all (its helpers were declared taking ``self`` but
called without one, and its serializer concatenated a str onto bytes), so a
pre-Sapling JoinSplit would have raised or been misread.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from pyzcash.consensus import Zatoshi
from pyzcash.errors import ParseError

if TYPE_CHECKING:
    from pyzcash.encoding import Reader, Writer

__all__ = [
    "GROTH16_PROOF_LEN",
    "NOTE_CIPHERTEXT_LEN",
    "NUM_JS_INPUTS",
    "NUM_JS_OUTPUTS",
    "PHGR_PROOF_LEN",
    "Groth16Proof",
    "JoinSplit",
    "PHGRProof",
    "SproutProof",
]

NUM_JS_INPUTS = 2
NUM_JS_OUTPUTS = 2
NOTE_CIPHERTEXT_LEN = 601
"""1 leading + 8 value + 32 rho + 32 r + 512 memo + 16 auth bytes."""

GROTH16_PROOF_LEN = 192
PHGR_PROOF_LEN = 296
"""Seven compressed G1 points (33 bytes each) and one G2 point (65)."""

_G1_PREFIX_MASK = 0x02
_G2_PREFIX_MASK = 0x0A


@dataclass(frozen=True, slots=True)
class Groth16Proof:
    """A Groth16 proof, carried opaquely.

    This library does not verify proofs, so a proof is 192 bytes that must
    survive a round trip and nothing more.
    """

    data: bytes

    def __post_init__(self) -> None:
        if len(self.data) != GROTH16_PROOF_LEN:
            raise ParseError(
                f"a Groth16 proof is {GROTH16_PROOF_LEN} bytes, "
                f"got {len(self.data)}"
            )

    @classmethod
    def read(cls, reader: Reader) -> Self:
        return cls(reader.read(GROTH16_PROOF_LEN))

    def write(self, writer: Writer) -> None:
        writer.write(self.data)


@dataclass(frozen=True, slots=True)
class PHGRProof:
    """A PHGR13 proof, as carried by pre-Sapling JoinSplits.

    Each point is stored with the leading byte the wire used, so that a proof
    re-serializes to the exact bytes it was read from. The leading byte encodes
    a prefix mask together with one bit of the omitted y coordinate, and
    rebuilding it from a normalized form would risk changing it.
    """

    g_a: bytes
    g_a_prime: bytes
    g_b: bytes
    g_b_prime: bytes
    g_c: bytes
    g_c_prime: bytes
    g_k: bytes
    g_h: bytes

    @classmethod
    def read(cls, reader: Reader) -> Self:
        return cls(
            g_a=_read_g1(reader),
            g_a_prime=_read_g1(reader),
            g_b=_read_g2(reader),
            g_b_prime=_read_g1(reader),
            g_c=_read_g1(reader),
            g_c_prime=_read_g1(reader),
            g_k=_read_g1(reader),
            g_h=_read_g1(reader),
        )

    def write(self, writer: Writer) -> None:
        for point in (
            self.g_a,
            self.g_a_prime,
            self.g_b,
            self.g_b_prime,
            self.g_c,
            self.g_c_prime,
            self.g_k,
            self.g_h,
        ):
            writer.write(point)


SproutProof = Groth16Proof | PHGRProof


def _read_g1(reader: Reader) -> bytes:
    """A compressed G1 point: a prefix-and-sign byte, then a 32-byte x."""
    leading = reader.read_u8()
    if leading & ~1 != _G1_PREFIX_MASK:
        raise ParseError(
            f"invalid G1 point prefix 0x{leading:02x} in a PHGR13 proof"
        )
    return bytes([leading]) + reader.read(32)


def _read_g2(reader: Reader) -> bytes:
    """A compressed G2 point: a prefix-and-sign byte, then a 64-byte x."""
    leading = reader.read_u8()
    if leading & ~1 != _G2_PREFIX_MASK:
        raise ParseError(
            f"invalid G2 point prefix 0x{leading:02x} in a PHGR13 proof"
        )
    return bytes([leading]) + reader.read(64)


@dataclass(frozen=True, slots=True)
class JoinSplit:
    """One JoinSplit: up to two shielded inputs and two shielded outputs.

    ``vpub_old`` moves value *into* the shielded pool and ``vpub_new`` moves it
    out. They are the only parts of a JoinSplit that are not hidden.
    """

    vpub_old: Zatoshi
    vpub_new: Zatoshi
    anchor: bytes
    nullifiers: tuple[bytes, ...]
    commitments: tuple[bytes, ...]
    ephemeral_key: bytes
    random_seed: bytes
    macs: tuple[bytes, ...]
    proof: SproutProof
    ciphertexts: tuple[bytes, ...]

    @classmethod
    def read(cls, reader: Reader, *, groth16: bool) -> Self:
        """Read a JoinSplit.

        Args:
            groth16: which proof system to expect. It is not recorded in the
                JoinSplit itself: v4 transactions carry Groth16 proofs, and
                earlier ones carry PHGR13.
        """
        vpub_old = Zatoshi(reader.read_i64())
        vpub_new = Zatoshi(reader.read_i64())
        anchor = reader.read_u256()
        nullifiers = tuple(reader.read_u256() for _ in range(NUM_JS_INPUTS))
        commitments = tuple(reader.read_u256() for _ in range(NUM_JS_OUTPUTS))
        ephemeral_key = reader.read_u256()
        random_seed = reader.read_u256()
        macs = tuple(reader.read_u256() for _ in range(NUM_JS_INPUTS))
        proof: SproutProof = (
            Groth16Proof.read(reader) if groth16 else PHGRProof.read(reader)
        )
        ciphertexts = tuple(
            reader.read(NOTE_CIPHERTEXT_LEN) for _ in range(NUM_JS_OUTPUTS)
        )
        return cls(
            vpub_old=vpub_old,
            vpub_new=vpub_new,
            anchor=anchor,
            nullifiers=nullifiers,
            commitments=commitments,
            ephemeral_key=ephemeral_key,
            random_seed=random_seed,
            macs=macs,
            proof=proof,
            ciphertexts=ciphertexts,
        )

    def write(self, writer: Writer) -> None:
        writer.write_i64(self.vpub_old.value)
        writer.write_i64(self.vpub_new.value)
        writer.write_u256(self.anchor)
        for nullifier in self.nullifiers:
            writer.write_u256(nullifier)
        for commitment in self.commitments:
            writer.write_u256(commitment)
        writer.write_u256(self.ephemeral_key)
        writer.write_u256(self.random_seed)
        for mac in self.macs:
            writer.write_u256(mac)
        self.proof.write(writer)
        for ciphertext in self.ciphertexts:
            writer.write(ciphertext)
