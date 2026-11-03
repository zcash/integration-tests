"""Zcash addresses: transparent, Sapling, unified (ZIP 316), and TEX (ZIP 320).

The entry point is :func:`parse_address`, which returns a tagged union. Match on
the result rather than inspecting strings:

    >>> from pyzcash import Network
    >>> from pyzcash.address import parse_address, UnifiedAddress
    >>> address = parse_address(some_string, Network.MAIN)
    >>> match address:
    ...     case UnifiedAddress(receivers=receivers):
    ...         ...  # pick the best pool both sides support

Every decoder is strict. An address that parses here is one that a conforming
wallet could have produced; the structural rules of ZIP 316 in particular are
enforced rather than assumed.
"""

from __future__ import annotations

from pyzcash.address.params import AddressParams, params_for
from pyzcash.address.sapling import (
    DIVERSIFIER_LEN,
    SAPLING_ADDRESS_LEN,
    SaplingAddress,
)
from pyzcash.address.transparent import (
    HASH_LEN,
    TexAddress,
    TransparentAddress,
    TransparentKind,
)
from pyzcash.address.unified import (
    ORCHARD_ADDRESS_LEN,
    PADDING_LEN,
    Receiver,
    ReceiverType,
    UnifiedAddress,
    UnknownReceiver,
)
from pyzcash.consensus import Network
from pyzcash.errors import ChecksumError, ParseError, ZcashError

__all__ = [
    "DIVERSIFIER_LEN",
    "HASH_LEN",
    "ORCHARD_ADDRESS_LEN",
    "PADDING_LEN",
    "SAPLING_ADDRESS_LEN",
    "Address",
    "AddressParams",
    "Receiver",
    "ReceiverType",
    "SaplingAddress",
    "TexAddress",
    "TransparentAddress",
    "TransparentKind",
    "UnifiedAddress",
    "UnknownReceiver",
    "params_for",
    "parse_address",
]

Address = TransparentAddress | SaplingAddress | UnifiedAddress | TexAddress
"""Any address this library can parse."""


def parse_address(encoded: str, network: Network) -> Address:
    """Parse any Zcash address, returning a tagged union.

    The network must be given: testnet and regtest share their Base58 prefixes,
    so a transparent address does not carry enough information to identify its
    own network, and guessing would mean sometimes guessing wrong about where
    money is going.

    Raises:
        ParseError: if the string is not a valid address on ``network``. The
            message names the form that was attempted, when the prefix makes
            that unambiguous.
    """
    if not encoded:
        raise ParseError("an empty string is not an address")

    params = params_for(network)

    # Bech32-family prefixes are unambiguous, so dispatch on them and let the
    # specific decoder produce a precise error rather than a generic one.
    separator = encoded.rfind("1")
    if separator > 0:
        hrp = encoded[:separator].lower()
        if hrp == params.hrp_unified_address:
            return UnifiedAddress.decode(encoded, network)
        if hrp == params.hrp_sapling_address:
            return SaplingAddress.decode(encoded, network)
        if hrp == params.hrp_tex_address:
            return TexAddress.decode(encoded, network)

    try:
        return TransparentAddress.decode(encoded, network)
    except ChecksumError:
        # A failing checksum is a precise, useful diagnosis: the string is the
        # right shape but was mistyped or truncated. Do not bury it in a
        # generic "not an address".
        raise
    except ZcashError as e:
        raise ParseError(
            f"not a valid {network.value} address: {_diagnose(encoded, params)}"
        ) from e


def _diagnose(encoded: str, params: AddressParams) -> str:
    """Explain why a string is not an address on this network."""
    separator = encoded.rfind("1")
    if separator > 0:
        hrp = encoded[:separator].lower()
        for other in Network:
            other_params = params_for(other)
            if hrp in (
                other_params.hrp_unified_address,
                other_params.hrp_sapling_address,
                other_params.hrp_tex_address,
            ):
                return (
                    f"the prefix {hrp!r} is a {other.value} address, not a "
                    f"{params.hrp_unified_address!r}-family one"
                )
    return (
        "it is neither a Base58Check transparent address nor a known "
        "Bech32 prefix"
    )
