"""Unified addresses (ZIP 316).

A unified address is a list of receivers, one per pool the recipient can accept
funds in, so a sender picks the best pool they both support. The encoding is
Bech32m over an F4Jumbled, HRP-padded list of (typecode, length, data) items.

The structural rules below are not decoration. F4Jumble exists so that a
receiver cannot be stripped out; the ordering and duplicate rules exist so that
one address has exactly one encoding; and the "at least one shielded receiver"
rule is why ZIP 320 TEX addresses had to be invented for the transparent-only
case. A decoder that skips these checks will happily accept addresses that no
conforming wallet would produce, so this one enforces them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING, Self

from pyzcash.address.params import params_for
from pyzcash.address.sapling import SAPLING_ADDRESS_LEN
from pyzcash.address.transparent import HASH_LEN
from pyzcash.encoding import (
    Bech32Encoding,
    Reader,
    Writer,
    bech32_decode,
    bech32_encode,
    f4jumble,
    f4jumble_inverse,
)
from pyzcash.errors import ParseError

if TYPE_CHECKING:
    from pyzcash.consensus import Network

__all__ = [
    "ORCHARD_ADDRESS_LEN",
    "PADDING_LEN",
    "Receiver",
    "ReceiverType",
    "UnifiedAddress",
    "UnknownReceiver",
]

PADDING_LEN = 16
"""ZIP 316 pads the payload with the HRP, zero-extended to 16 bytes."""

ORCHARD_ADDRESS_LEN = 43
"""An Orchard raw address is the same size as a Sapling one."""


class ReceiverType(IntEnum):
    """The receiver typecodes ZIP 316 assigns."""

    P2PKH = 0x00
    P2SH = 0x01
    SAPLING = 0x02
    ORCHARD = 0x03

    @property
    def is_shielded(self) -> bool:
        return self in (ReceiverType.SAPLING, ReceiverType.ORCHARD)

    @property
    def is_transparent(self) -> bool:
        return self in (ReceiverType.P2PKH, ReceiverType.P2SH)


_EXPECTED_LENGTHS: dict[ReceiverType, int] = {
    ReceiverType.P2PKH: HASH_LEN,
    ReceiverType.P2SH: HASH_LEN,
    ReceiverType.SAPLING: SAPLING_ADDRESS_LEN,
    ReceiverType.ORCHARD: ORCHARD_ADDRESS_LEN,
}


@dataclass(frozen=True, slots=True)
class Receiver:
    """A receiver of a type this library knows."""

    type: ReceiverType
    data: bytes

    def __post_init__(self) -> None:
        expected = _EXPECTED_LENGTHS[self.type]
        if len(self.data) != expected:
            raise ParseError(
                f"a {self.type.name} receiver is {expected} bytes, "
                f"got {len(self.data)}"
            )

    @property
    def typecode(self) -> int:
        return int(self.type)


@dataclass(frozen=True, slots=True)
class UnknownReceiver:
    """A receiver whose typecode this library does not know.

    Kept rather than discarded: a future pool's receiver must survive a decode
    and re-encode, or wallets that do not yet know about it would silently
    rewrite addresses into a form the recipient did not choose.
    """

    typecode: int
    data: bytes


@dataclass(frozen=True, slots=True)
class UnifiedAddress:
    """A ZIP 316 unified address."""

    network: Network
    receivers: tuple[Receiver | UnknownReceiver, ...]

    def __post_init__(self) -> None:
        if not self.receivers:
            raise ParseError(
                "a unified address must have at least one receiver"
            )

        typecodes = [r.typecode for r in self.receivers]
        if len(set(typecodes)) != len(typecodes):
            raise ParseError(
                "a unified address must not repeat a receiver typecode"
            )
        if typecodes != sorted(typecodes):
            raise ParseError(
                "unified address receivers must be in ascending typecode order"
            )

        known = [r.type for r in self.receivers if isinstance(r, Receiver)]
        if not any(t.is_shielded for t in known):
            raise ParseError(
                "a unified address must contain at least one shielded "
                "receiver; a transparent-only address is a ZIP 320 TEX address"
            )
        if sum(1 for t in known if t.is_transparent) > 1:
            raise ParseError(
                "a unified address may contain at most one transparent receiver"
            )

    def receiver(self, receiver_type: ReceiverType) -> Receiver | None:
        """The receiver of this type, if the address carries one."""
        for r in self.receivers:
            if isinstance(r, Receiver) and r.type is receiver_type:
                return r
        return None

    @property
    def has_orchard(self) -> bool:
        return self.receiver(ReceiverType.ORCHARD) is not None

    @property
    def has_sapling(self) -> bool:
        return self.receiver(ReceiverType.SAPLING) is not None

    def encode(self) -> str:
        hrp = params_for(self.network).hrp_unified_address
        writer = Writer()
        for r in self.receivers:
            writer.write_compact_size(r.typecode)
            writer.write_bytes_compact(r.data)
        writer.write(_padding(hrp))
        return bech32_encode(
            hrp, f4jumble(writer.to_bytes()), Bech32Encoding.BECH32M
        )

    @classmethod
    def decode(cls, encoded: str, network: Network) -> Self:
        """Decode a unified address, enforcing every ZIP 316 structural rule.

        Raises:
            ParseError: on the wrong network prefix, Bech32 instead of Bech32m,
                corrupt padding, a receiver of the wrong length, repeated or
                out-of-order typecodes, no shielded receiver, or more than one
                transparent receiver.
            ChecksumError: on a bad Bech32m checksum.
        """
        hrp, payload, encoding = bech32_decode(encoded)
        expected_hrp = params_for(network).hrp_unified_address
        if hrp != expected_hrp:
            raise ParseError(
                f"expected a {network.value} unified address ({expected_hrp}), "
                f"got prefix {hrp!r}"
            )
        if encoding is not Bech32Encoding.BECH32M:
            raise ParseError("a unified address must be Bech32m, not Bech32")

        plaintext = f4jumble_inverse(payload)
        if plaintext[-PADDING_LEN:] != _padding(hrp):
            raise ParseError(
                "the unified address padding does not match its prefix, so the "
                "payload is corrupt"
            )

        reader = Reader(plaintext[:-PADDING_LEN])
        receivers: list[Receiver | UnknownReceiver] = []
        while reader.remaining:
            typecode = reader.read_compact_size()
            data = reader.read_bytes_compact()
            try:
                known = ReceiverType(typecode)
            except ValueError:
                receivers.append(UnknownReceiver(typecode=typecode, data=data))
            else:
                receivers.append(Receiver(type=known, data=data))

        return cls(network=network, receivers=tuple(receivers))


def _padding(hrp: str) -> bytes:
    raw = hrp.encode("utf-8")
    if len(raw) > PADDING_LEN:
        raise ParseError(f"the prefix {hrp!r} is too long to pad")
    return raw + b"\x00" * (PADDING_LEN - len(raw))
