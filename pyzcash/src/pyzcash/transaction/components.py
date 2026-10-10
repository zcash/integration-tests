"""The transparent parts of a transaction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self

from pyzcash.consensus import Zatoshi
from pyzcash.script import Script

if TYPE_CHECKING:
    from pyzcash.encoding import Reader, Writer

__all__ = ["SEQUENCE_FINAL", "OutPoint", "TxIn", "TxOut"]

SEQUENCE_FINAL = 0xFFFFFFFF
"""The sequence number of an input that does not opt into lock-time."""

_COINBASE_TXID = bytes(32)
_COINBASE_INDEX = 0xFFFFFFFF


@dataclass(frozen=True, slots=True)
class OutPoint:
    """A reference to a previous transaction's output.

    ``txid`` is in *internal* byte order, which is what the wire carries. Block
    explorers and RPC show the reverse. :meth:`txid_hex` gives the displayed
    form, so that the two orders never get quietly confused.
    """

    txid: bytes
    index: int

    def __post_init__(self) -> None:
        if len(self.txid) != 32:
            raise ValueError(f"a txid is 32 bytes, got {len(self.txid)}")

    @property
    def is_coinbase(self) -> bool:
        """A coinbase input references a null outpoint."""
        return self.txid == _COINBASE_TXID and self.index == _COINBASE_INDEX

    def txid_hex(self) -> str:
        """The txid as it is displayed: byte-reversed."""
        return self.txid[::-1].hex()

    @classmethod
    def read(cls, reader: Reader) -> Self:
        return cls(txid=reader.read_u256(), index=reader.read_u32())

    def write(self, writer: Writer) -> None:
        writer.write_u256(self.txid).write_u32(self.index)


@dataclass(frozen=True, slots=True)
class TxIn:
    """A transparent input."""

    prevout: OutPoint
    script_sig: Script = field(default_factory=Script)
    sequence: int = SEQUENCE_FINAL

    @property
    def is_coinbase(self) -> bool:
        return self.prevout.is_coinbase

    @classmethod
    def read(cls, reader: Reader) -> Self:
        return cls(
            prevout=OutPoint.read(reader),
            script_sig=Script(reader.read_bytes_compact()),
            sequence=reader.read_u32(),
        )

    def write(self, writer: Writer) -> None:
        self.prevout.write(writer)
        writer.write_bytes_compact(self.script_sig.raw).write_u32(self.sequence)


@dataclass(frozen=True, slots=True)
class TxOut:
    """A transparent output."""

    value: Zatoshi
    script_pubkey: Script

    @classmethod
    def read(cls, reader: Reader) -> Self:
        return cls(
            value=Zatoshi(reader.read_i64()),
            script_pubkey=Script(reader.read_bytes_compact()),
        )

    def write(self, writer: Writer) -> None:
        writer.write_i64(self.value.value)
        writer.write_bytes_compact(self.script_pubkey.raw)
