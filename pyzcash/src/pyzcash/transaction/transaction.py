"""The transaction itself, across every version Zcash has shipped."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Self

from pyzcash.consensus import Zatoshi
from pyzcash.encoding import Reader, Writer, sha256d
from pyzcash.errors import ParseError
from pyzcash.transaction.components import TxIn, TxOut
from pyzcash.transaction.orchard import OrchardBundle
from pyzcash.transaction.sapling import SaplingBundle
from pyzcash.transaction.sprout import JoinSplit

__all__ = [
    "OVERWINTER_VERSION_GROUP_ID",
    "SAPLING_VERSION_GROUP_ID",
    "SPROUT_VERSION_GROUP_ID",
    "ZIP225_VERSION_GROUP_ID",
    "Transaction",
    "TxVersion",
]

SPROUT_VERSION_GROUP_ID = 0x00000000
OVERWINTER_VERSION_GROUP_ID = 0x03C48270
SAPLING_VERSION_GROUP_ID = 0x892F2085
ZIP225_VERSION_GROUP_ID = 0x26A7270A

_OVERWINTERED_FLAG = 1 << 31
_VERSION_MASK = 0x7FFFFFFF
_JOINSPLIT_SIG_LEN = 64


class TxVersion(Enum):
    """A transaction version, identified by the pair that actually decides it.

    The version number alone is not enough: a transaction is identified by the
    overwintered flag, the version group ID, and the version together. A v4
    version number with the wrong version group ID is not a Sapling transaction,
    and treating it as one would misread every byte after the header.
    """

    V1 = (False, SPROUT_VERSION_GROUP_ID, 1)
    V2 = (False, SPROUT_VERSION_GROUP_ID, 2)
    V3 = (True, OVERWINTER_VERSION_GROUP_ID, 3)
    V4 = (True, SAPLING_VERSION_GROUP_ID, 4)
    V5 = (True, ZIP225_VERSION_GROUP_ID, 5)

    @property
    def overwintered(self) -> bool:
        return self.value[0]

    @property
    def version_group_id(self) -> int:
        return self.value[1]

    @property
    def number(self) -> int:
        return self.value[2]

    @property
    def has_joinsplits(self) -> bool:
        """Sprout JoinSplits are carried by v2 through v4."""
        return self in (TxVersion.V2, TxVersion.V3, TxVersion.V4)

    @property
    def joinsplits_use_groth16(self) -> bool:
        """Sapling-era transactions carry Groth16; earlier ones PHGR13."""
        return self is TxVersion.V4

    @property
    def has_expiry_height(self) -> bool:
        return self.overwintered

    @classmethod
    def identify(
        cls, overwintered: bool, version_group_id: int, number: int
    ) -> Self:
        for candidate in cls:
            if candidate.value == (overwintered, version_group_id, number):
                return candidate
        raise ParseError(
            f"unknown transaction version: overwintered={overwintered}, "
            f"version group ID 0x{version_group_id:08x}, version {number}. "
            f"This library does not know how to read it, and guessing at a "
            f"layout would misreport what the transaction says."
        )


@dataclass(frozen=True, slots=True)
class Transaction:
    """A Zcash transaction.

    Parsing is exact and total: :meth:`from_bytes` either produces a transaction
    that re-serializes to the identical bytes, or it raises. It never guesses at
    an unknown layout, and it never leaves trailing bytes unread.
    """

    version: TxVersion
    lock_time: int = 0
    expiry_height: int | None = None
    consensus_branch_id: int | None = None
    vin: tuple[TxIn, ...] = ()
    vout: tuple[TxOut, ...] = ()
    joinsplits: tuple[JoinSplit, ...] = ()
    joinsplit_pubkey: bytes | None = None
    joinsplit_sig: bytes | None = None
    sapling_bundle: SaplingBundle = field(default_factory=SaplingBundle)
    orchard_bundle: OrchardBundle = field(default_factory=OrchardBundle)

    # --- identity ------------------------------------------------------------

    @property
    def is_coinbase(self) -> bool:
        return bool(self.vin) and self.vin[0].is_coinbase

    def legacy_txid(self) -> bytes:
        """The txid of a pre-v5 transaction: double SHA-256 of its bytes.

        For v5 and later the txid is a ZIP 244 digest over the transaction's
        effects, not a hash of the serialization, so this raises rather than
        returning a plausible but wrong value.
        """
        if self.version is TxVersion.V5:
            raise ParseError(
                "a v5 transaction's txid is the ZIP 244 digest, not a hash of "
                "its serialization"
            )
        return sha256d(self.to_bytes())

    def legacy_txid_hex(self) -> str:
        """The legacy txid as displayed: byte-reversed."""
        return self.legacy_txid()[::-1].hex()

    # --- serialization -------------------------------------------------------

    @classmethod
    def from_hex(cls, hex_str: str) -> Self:
        return cls.from_bytes(bytes.fromhex(hex_str))

    @classmethod
    def from_bytes(cls, data: bytes) -> Self:
        reader = Reader(data)
        tx = cls.read(reader)
        reader.expect_exhausted()
        return tx

    def to_hex(self) -> str:
        return self.to_bytes().hex()

    def to_bytes(self) -> bytes:
        writer = Writer()
        self.write(writer)
        return writer.to_bytes()

    @classmethod
    def read(cls, reader: Reader) -> Self:
        header = reader.read_u32()
        overwintered = bool(header & _OVERWINTERED_FLAG)
        number = header & _VERSION_MASK
        version_group_id = (
            reader.read_u32() if overwintered else SPROUT_VERSION_GROUP_ID
        )
        version = TxVersion.identify(overwintered, version_group_id, number)

        if version is TxVersion.V5:
            return cls._read_v5(reader, version)
        return cls._read_legacy(reader, version)

    @classmethod
    def _read_v5(cls, reader: Reader, version: TxVersion) -> Self:
        consensus_branch_id = reader.read_u32()
        lock_time = reader.read_u32()
        expiry_height = reader.read_u32()

        vin = tuple(
            TxIn.read(reader) for _ in range(reader.read_compact_size())
        )
        vout = tuple(
            TxOut.read(reader) for _ in range(reader.read_compact_size())
        )

        return cls(
            version=version,
            consensus_branch_id=consensus_branch_id,
            lock_time=lock_time,
            expiry_height=expiry_height,
            vin=vin,
            vout=vout,
            sapling_bundle=SaplingBundle.read_v5(reader),
            orchard_bundle=OrchardBundle.read(reader),
        )

    @classmethod
    def _read_legacy(cls, reader: Reader, version: TxVersion) -> Self:
        vin = tuple(
            TxIn.read(reader) for _ in range(reader.read_compact_size())
        )
        vout = tuple(
            TxOut.read(reader) for _ in range(reader.read_compact_size())
        )
        lock_time = reader.read_u32()
        expiry_height = reader.read_u32() if version.has_expiry_height else None

        sapling = SaplingBundle()
        if version is TxVersion.V4:
            value_balance = Zatoshi(reader.read_i64())
            spends = tuple(
                SaplingBundle.read_v4_spend(reader)
                for _ in range(reader.read_compact_size())
            )
            outputs = tuple(
                SaplingBundle.read_v4_output(reader)
                for _ in range(reader.read_compact_size())
            )
            sapling = SaplingBundle(
                spends=spends, outputs=outputs, value_balance=value_balance
            )

        joinsplits: tuple[JoinSplit, ...] = ()
        joinsplit_pubkey: bytes | None = None
        joinsplit_sig: bytes | None = None
        if version.has_joinsplits:
            joinsplits = tuple(
                JoinSplit.read(reader, groth16=version.joinsplits_use_groth16)
                for _ in range(reader.read_compact_size())
            )
            if joinsplits:
                joinsplit_pubkey = reader.read_u256()
                joinsplit_sig = reader.read(_JOINSPLIT_SIG_LEN)

        # The binding signature comes after the JoinSplits, and only exists when
        # the Sapling bundle is non-empty.
        if version is TxVersion.V4 and not sapling.is_empty:
            sapling = SaplingBundle(
                spends=sapling.spends,
                outputs=sapling.outputs,
                value_balance=sapling.value_balance,
                binding_sig=reader.read(64),
            )

        return cls(
            version=version,
            lock_time=lock_time,
            expiry_height=expiry_height,
            vin=vin,
            vout=vout,
            joinsplits=joinsplits,
            joinsplit_pubkey=joinsplit_pubkey,
            joinsplit_sig=joinsplit_sig,
            sapling_bundle=sapling,
        )

    def write(self, writer: Writer) -> None:
        header = self.version.number
        if self.version.overwintered:
            header |= _OVERWINTERED_FLAG
        writer.write_u32(header)
        if self.version.overwintered:
            writer.write_u32(self.version.version_group_id)

        if self.version is TxVersion.V5:
            self._write_v5(writer)
        else:
            self._write_legacy(writer)

    def _write_v5(self, writer: Writer) -> None:
        if self.consensus_branch_id is None or self.expiry_height is None:
            raise ParseError(
                "a v5 transaction needs a consensus branch ID and an expiry "
                "height"
            )
        writer.write_u32(self.consensus_branch_id)
        writer.write_u32(self.lock_time)
        writer.write_u32(self.expiry_height)

        writer.write_compact_size(len(self.vin))
        for txin in self.vin:
            txin.write(writer)
        writer.write_compact_size(len(self.vout))
        for txout in self.vout:
            txout.write(writer)

        self.sapling_bundle.write_v5(writer)
        self.orchard_bundle.write(writer)

    def _write_legacy(self, writer: Writer) -> None:
        writer.write_compact_size(len(self.vin))
        for txin in self.vin:
            txin.write(writer)
        writer.write_compact_size(len(self.vout))
        for txout in self.vout:
            txout.write(writer)
        writer.write_u32(self.lock_time)

        if self.version.has_expiry_height:
            if self.expiry_height is None:
                raise ParseError(
                    f"a {self.version.name} transaction needs an expiry height"
                )
            writer.write_u32(self.expiry_height)

        sapling = self.sapling_bundle
        if self.version is TxVersion.V4:
            writer.write_i64(sapling.value_balance.value)
            writer.write_compact_size(len(sapling.spends))
            for spend in sapling.spends:
                SaplingBundle.write_v4_spend(writer, spend)
            writer.write_compact_size(len(sapling.outputs))
            for output in sapling.outputs:
                SaplingBundle.write_v4_output(writer, output)
        elif not sapling.is_empty:
            raise ParseError(
                f"a {self.version.name} transaction cannot carry a Sapling "
                f"bundle"
            )

        if self.version.has_joinsplits:
            writer.write_compact_size(len(self.joinsplits))
            for joinsplit in self.joinsplits:
                joinsplit.write(writer)
            if self.joinsplits:
                if self.joinsplit_pubkey is None or self.joinsplit_sig is None:
                    raise ParseError(
                        "a transaction with JoinSplits needs a JoinSplit "
                        "public key and signature"
                    )
                writer.write_u256(self.joinsplit_pubkey)
                writer.write(self.joinsplit_sig)
        elif self.joinsplits:
            raise ParseError(
                f"a {self.version.name} transaction cannot carry JoinSplits"
            )

        if self.version is TxVersion.V4 and not sapling.is_empty:
            if sapling.binding_sig is None:
                raise ParseError(
                    "a non-empty Sapling bundle needs a binding signature"
                )
            writer.write(sapling.binding_sig)
