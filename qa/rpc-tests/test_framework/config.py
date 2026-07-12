#!/usr/bin/env python3
# Copyright (c) 2025 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

from dataclasses import dataclass, field
from typing import Any

@dataclass
class ZebraArgs:
    miner_address: str = "tmSRd1r8gs77Ja67Fw1JcdoXytxsyrLTPJm"
    # Explicitly activate every network upgrade at height 1, matching zallet.toml's
    # regtest defaults. Zebra only activates the NU5+ upgrades that are listed here
    # (it does not imply later ones), so each must be named or zallet will reject the
    # coinbase that zebra mines: zallet expects the latest upgrade's consensus branch
    # ID while zebra commits to a lower one. Omitting NU5 also makes create_cache.py
    # fail with "Missing Orchard tree state".
    activation_heights: dict[str, int] = field(
        default_factory=lambda: {"NU5": 1, "NU6": 1, "NU6.1": 1, "NU6.2": 1}
    )
    funding_streams: list[dict[str, Any]] = field(default_factory=list)
    lockbox_disbursements: list[dict[str, Any]] = field(default_factory=list)
    should_allow_unshielded_coinbase_spends: bool | None = None

    def __add__(self, other):
        if other is None:
            return self

        defaults = ZebraArgs()
        if other.miner_address != defaults.miner_address:
            self.miner_address = other.miner_address
        if other.activation_heights != defaults.activation_heights:
            self.activation_heights = other.activation_heights
        if other.funding_streams != defaults.funding_streams:
            self.funding_streams = other.funding_streams
        if other.lockbox_disbursements != defaults.lockbox_disbursements:
            self.lockbox_disbursements = other.lockbox_disbursements
        if other.should_allow_unshielded_coinbase_spends != defaults.should_allow_unshielded_coinbase_spends:
            self.should_allow_unshielded_coinbase_spends = other.should_allow_unshielded_coinbase_spends
        return self


# Network-upgrade name -> consensus branch id (hex string), in activation
# order. Branch ids must match those in
# qa/rpc-tests/test_framework/util.py. Names accept both dotted and
# underscore spellings (e.g. "NU6.1" / "NU6_1").
_NU_BRANCH_IDS = [
    ("Overwinter", "5ba81b19"),
    ("Sapling", "76b809bb"),
    ("Blossom", "2bb40e60"),
    ("Heartwood", "f5b9230b"),
    ("Canopy", "e9ff75a6"),
    ("NU5", "c2d6d0b4"),
    ("NU6", "c8e71055"),
    ("NU6.1", "4dec4df0"),
    ("NU6.2", "5437f330"),
    ("NU6.3", "37a5165b"),
]

# Upgrades before NU5. Zebra regtest activates these at height 1 when a later
# upgrade is configured, so we emit them explicitly to keep zallet in sync.
_PRE_NU5_NAMES = {"Overwinter", "Sapling", "Blossom", "Heartwood", "Canopy"}


def render_regtest_nuparams(activation_heights):
    """
    Translate a {NU name: height} dict (the same shape as
    ZebraArgs.activation_heights) into zallet's `regtest_nuparams` list,
    i.e. ["<branch-id hex>:<height>", ...].
    """
    requested = {
        name.replace("_", "."): height
        for name, height in (activation_heights or {}).items()
    }
    nuparams = []
    for name, branch_id in _NU_BRANCH_IDS:
        if name in requested:
            height = requested[name]
        elif name in _PRE_NU5_NAMES:
            height = 1
        else:
            continue
        nuparams.append("%s:%d" % (branch_id, height))
    return nuparams


@dataclass
class ZalletArgs:
    # Network-upgrade activation heights for the wallet, using the same
    # {NU name: height} shape as ZebraArgs.activation_heights so the wallet
    # can be configured to match the node.
    activation_heights: dict[str, int] = field(default_factory=dict)

    # The ZIP 32 seed fingerprint (bech32m, as `z_listaccounts` reports it) of
    # the `zcashd` wallet whose legacy pool of funds this wallet holds, enabling
    # `zcashd`'s legacy semantics for it. Setting it is what makes `ANY_TADDR`
    # spendable in `z_sendmany`. A wallet's seed is generated when the wallet is
    # created, so a test learns the fingerprint from the running wallet and then
    # restarts it with this set.
    legacy_pool_seed_fingerprint: str | None = None

    def __add__(self, other):
        if other is None:
            return self

        defaults = ZalletArgs()
        if other.activation_heights != defaults.activation_heights:
            self.activation_heights = other.activation_heights
        if other.legacy_pool_seed_fingerprint != defaults.legacy_pool_seed_fingerprint:
            self.legacy_pool_seed_fingerprint = other.legacy_pool_seed_fingerprint
        return self


@dataclass
class ZebraConfig:
    network_listen_address: str = "127.0.0.1:0"
    rpc_listen_address: str = "127.0.0.1:0"
    data_dir: str | None = None
    indexer_listen_address: str = "127.0.0.1:0"
    extra_args: ZebraArgs | None = None

    def update(self, config_file):
        # Base config updates
        config_file['rpc']['listen_addr'] = self.rpc_listen_address
        config_file['rpc']['indexer_listen_addr'] = self.indexer_listen_address
        config_file['network']['listen_addr'] = self.network_listen_address
        config_file['state']['cache_dir'] = self.data_dir

        # Extra args updates
        extra_args = self.extra_args or ZebraArgs()
        config_file['mining']['miner_address'] = extra_args.miner_address
        config_file['network']['testnet_parameters']['funding_streams'] = extra_args.funding_streams
        config_file['network']['testnet_parameters']['activation_heights'] = extra_args.activation_heights
        config_file['network']['testnet_parameters']['lockbox_disbursements'] = extra_args.lockbox_disbursements

        return config_file

@dataclass
class ZainoConfig:
    json_rpc_listen_address: str = "127.0.0.1:0"
    grpc_listen_address: str = "127.0.0.1:0"
    validator_grpc_listen_address: str = "127.0.0.1:0"
    validator_jsonrpc_listen_address: str = "127.0.0.1:0"

    def update(self, config_file):
        # Base config updates
        config_file['json_server_settings']['json_rpc_listen_address'] = self.json_rpc_listen_address
        config_file['grpc_settings']['grpc_listen_address'] = self.grpc_listen_address
        config_file['validator_settings']['validator_grpc_listen_address'] = self.validator_grpc_listen_address
        config_file['validator_settings']['validator_jsonrpc_listen_address'] = self.validator_jsonrpc_listen_address

        return config_file
