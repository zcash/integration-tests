"""Per-network address encoding constants.

Taken from librustzcash (components/zcash_protocol/src/constants/{mainnet,
testnet,regtest}.rs). Note that testnet and regtest share their Base58 prefixes
but not their Bech32 prefixes, so a transparent address alone cannot tell you
which of the two it belongs to, while a shielded one can.
"""

from __future__ import annotations

from dataclasses import dataclass

from pyzcash.consensus import Network

__all__ = ["AddressParams", "params_for"]


@dataclass(frozen=True, slots=True)
class AddressParams:
    """The encoding constants for one network."""

    coin_type: int
    b58_pubkey_prefix: bytes
    b58_script_prefix: bytes
    b58_secret_key_prefix: bytes
    b58_sprout_prefix: bytes
    hrp_sapling_address: str
    hrp_sapling_extended_fvk: str
    hrp_sapling_extended_spending_key: str
    hrp_unified_address: str
    hrp_unified_fvk: str
    hrp_unified_ivk: str
    hrp_tex_address: str


_MAIN = AddressParams(
    coin_type=133,
    b58_pubkey_prefix=b"\x1c\xb8",
    b58_script_prefix=b"\x1c\xbd",
    b58_secret_key_prefix=b"\x80",
    b58_sprout_prefix=b"\x16\x9a",
    hrp_sapling_address="zs",
    hrp_sapling_extended_fvk="zxviews",
    hrp_sapling_extended_spending_key="secret-extended-key-main",
    hrp_unified_address="u",
    hrp_unified_fvk="uview",
    hrp_unified_ivk="uivk",
    hrp_tex_address="tex",
)

_TEST = AddressParams(
    coin_type=1,
    b58_pubkey_prefix=b"\x1d\x25",
    b58_script_prefix=b"\x1c\xba",
    b58_secret_key_prefix=b"\xef",
    b58_sprout_prefix=b"\x16\xb6",
    hrp_sapling_address="ztestsapling",
    hrp_sapling_extended_fvk="zxviewtestsapling",
    hrp_sapling_extended_spending_key="secret-extended-key-test",
    hrp_unified_address="utest",
    hrp_unified_fvk="uviewtest",
    hrp_unified_ivk="uivktest",
    hrp_tex_address="textest",
)

# Regtest reuses testnet's Base58 prefixes but has its own Bech32 prefixes.
_REGTEST = AddressParams(
    coin_type=1,
    b58_pubkey_prefix=b"\x1d\x25",
    b58_script_prefix=b"\x1c\xba",
    b58_secret_key_prefix=b"\xef",
    b58_sprout_prefix=b"\x16\xb6",
    hrp_sapling_address="zregtestsapling",
    hrp_sapling_extended_fvk="zxviewregtestsapling",
    hrp_sapling_extended_spending_key="secret-extended-key-regtest",
    hrp_unified_address="uregtest",
    hrp_unified_fvk="uviewregtest",
    hrp_unified_ivk="uivkregtest",
    hrp_tex_address="texregtest",
)

_PARAMS: dict[Network, AddressParams] = {
    Network.MAIN: _MAIN,
    Network.TEST: _TEST,
    Network.REGTEST: _REGTEST,
}


def params_for(network: Network) -> AddressParams:
    """The address encoding constants for ``network``."""
    return _PARAMS[network]
