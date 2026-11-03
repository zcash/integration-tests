"""Transparent addresses (P2PKH and P2SH), and TEX addresses (ZIP 320)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Self

from pyzcash.address.params import params_for
from pyzcash.encoding import (
    Bech32Encoding,
    b58check_decode,
    b58check_encode,
    bech32_decode,
    bech32_encode,
)
from pyzcash.errors import ParseError

if TYPE_CHECKING:
    from pyzcash.consensus import Network

__all__ = ["HASH_LEN", "TexAddress", "TransparentAddress", "TransparentKind"]

HASH_LEN = 20
"""Both a public key hash and a script hash are 160 bits."""


class TransparentKind(Enum):
    """Which of the two transparent address forms this is."""

    P2PKH = "p2pkh"
    """Pay to public key hash: the hash is HASH160 of a public key."""

    P2SH = "p2sh"
    """Pay to script hash: the hash is HASH160 of a redeem script."""


@dataclass(frozen=True, slots=True)
class TransparentAddress:
    """A Base58Check transparent address.

    Testnet and regtest share Base58 prefixes, so decoding a transparent
    address cannot distinguish them. :func:`decode` therefore takes the network
    it is being read for, and verifies the prefix against it.
    """

    network: Network
    kind: TransparentKind
    hash: bytes

    def __post_init__(self) -> None:
        if len(self.hash) != HASH_LEN:
            raise ParseError(
                f"a transparent address hash is {HASH_LEN} bytes, "
                f"got {len(self.hash)}"
            )

    def encode(self) -> str:
        params = params_for(self.network)
        prefix = (
            params.b58_pubkey_prefix
            if self.kind is TransparentKind.P2PKH
            else params.b58_script_prefix
        )
        return b58check_encode(prefix + self.hash)

    @classmethod
    def decode(cls, encoded: str, network: Network) -> Self:
        """Decode a transparent address, checking it belongs to ``network``.

        Raises:
            ParseError: on a bad length, or a version prefix that belongs to a
                different network or to something other than an address.
        """
        payload = b58check_decode(encoded)
        params = params_for(network)
        prefix, body = payload[:2], payload[2:]
        if prefix == params.b58_pubkey_prefix:
            kind = TransparentKind.P2PKH
        elif prefix == params.b58_script_prefix:
            kind = TransparentKind.P2SH
        else:
            raise ParseError(
                f"version prefix {prefix.hex()} is not a {network.value} "
                f"transparent address"
            )
        return cls(network=network, kind=kind, hash=body)


@dataclass(frozen=True, slots=True)
class TexAddress:
    """A TEX address (ZIP 320): a transparent-only address, in Bech32m.

    A unified address must carry at least one shielded receiver, so there is no
    way to express "transparent only" as a UA. TEX fills that gap. It encodes
    the same 20-byte public key hash as a P2PKH address, but signals that the
    sender intends the funds to land transparently.
    """

    network: Network
    hash: bytes

    def __post_init__(self) -> None:
        if len(self.hash) != HASH_LEN:
            raise ParseError(
                f"a TEX address hash is {HASH_LEN} bytes, got {len(self.hash)}"
            )

    def encode(self) -> str:
        return bech32_encode(
            params_for(self.network).hrp_tex_address,
            self.hash,
            Bech32Encoding.BECH32M,
        )

    @classmethod
    def decode(cls, encoded: str, network: Network) -> Self:
        hrp, payload, encoding = bech32_decode(encoded)
        expected = params_for(network).hrp_tex_address
        if hrp != expected:
            raise ParseError(
                f"expected a {network.value} TEX address ({expected}), "
                f"got prefix {hrp!r}"
            )
        if encoding is not Bech32Encoding.BECH32M:
            raise ParseError("a TEX address must be Bech32m, not Bech32")
        return cls(network=network, hash=payload)

    def to_transparent(self) -> TransparentAddress:
        """The equivalent P2PKH address, where the funds actually land."""
        return TransparentAddress(
            network=self.network, kind=TransparentKind.P2PKH, hash=self.hash
        )
