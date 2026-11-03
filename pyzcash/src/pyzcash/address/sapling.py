"""Sapling payment addresses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from pyzcash.address.params import params_for
from pyzcash.encoding import Bech32Encoding, bech32_decode, bech32_encode
from pyzcash.errors import ParseError

if TYPE_CHECKING:
    from pyzcash.consensus import Network

__all__ = ["DIVERSIFIER_LEN", "SAPLING_ADDRESS_LEN", "SaplingAddress"]

DIVERSIFIER_LEN = 11
SAPLING_ADDRESS_LEN = 43
"""An 11-byte diversifier followed by a 32-byte diversified transmission key."""


@dataclass(frozen=True, slots=True)
class SaplingAddress:
    """A Sapling payment address: a diversifier and a transmission key.

    One spending key yields many addresses, one per diversifier, and they are
    unlinkable to anyone without the viewing key. That is why the diversifier
    is part of the address rather than derived from it.

    Sapling addresses are Bech32, not Bech32m: they predate BIP 350. Unified
    addresses, which came later, are Bech32m.
    """

    network: Network
    diversifier: bytes
    pk_d: bytes

    def __post_init__(self) -> None:
        if len(self.diversifier) != DIVERSIFIER_LEN:
            raise ParseError(
                f"a Sapling diversifier is {DIVERSIFIER_LEN} bytes, "
                f"got {len(self.diversifier)}"
            )
        if len(self.pk_d) != SAPLING_ADDRESS_LEN - DIVERSIFIER_LEN:
            raise ParseError(
                f"a Sapling transmission key is "
                f"{SAPLING_ADDRESS_LEN - DIVERSIFIER_LEN} bytes, "
                f"got {len(self.pk_d)}"
            )

    @property
    def raw(self) -> bytes:
        """The 43-byte encoding, as it appears inside a unified address."""
        return self.diversifier + self.pk_d

    @classmethod
    def from_raw(cls, raw: bytes, network: Network) -> Self:
        """Build from the 43-byte form, as carried inside a unified address."""
        if len(raw) != SAPLING_ADDRESS_LEN:
            raise ParseError(
                f"a Sapling address is {SAPLING_ADDRESS_LEN} bytes, "
                f"got {len(raw)}"
            )
        return cls(
            network=network,
            diversifier=raw[:DIVERSIFIER_LEN],
            pk_d=raw[DIVERSIFIER_LEN:],
        )

    def encode(self) -> str:
        return bech32_encode(
            params_for(self.network).hrp_sapling_address,
            self.raw,
            Bech32Encoding.BECH32,
        )

    @classmethod
    def decode(cls, encoded: str, network: Network) -> Self:
        hrp, payload, encoding = bech32_decode(encoded)
        expected = params_for(network).hrp_sapling_address
        if hrp != expected:
            raise ParseError(
                f"expected a {network.value} Sapling address ({expected}), "
                f"got prefix {hrp!r}"
            )
        if encoding is not Bech32Encoding.BECH32:
            raise ParseError("a Sapling address must be Bech32, not Bech32m")
        return cls.from_raw(payload, network)
