#!/usr/bin/env python3
# Copyright (c) 2025 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

from dataclasses import dataclass, field
from typing import Any

@dataclass
class ZebraArgs:
    miner_address: str = "tmSRd1r8gs77Ja67Fw1JcdoXytxsyrLTPJm"
    activation_heights: dict[str, int] = field(default_factory=dict)
    funding_streams: list[dict[str, Any]] = field(default_factory=list)
    lockbox_disbursements: list[dict[str, Any]] = field(default_factory=list)
    checkpoints: Any = None

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
        if other.checkpoints != defaults.checkpoints:
            self.checkpoints = other.checkpoints
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
        if extra_args.checkpoints is not None:
            config_file['network']['testnet_parameters']['checkpoints'] = extra_args.checkpoints
        else:
            config_file['network']['testnet_parameters'].pop('checkpoints', None)

        return config_file

@dataclass
class ZainoConfig:
    json_rpc_listen_address: str = "127.0.0.1:0"
    grpc_listen_address: str = "127.0.0.1:0"
    validator_grpc_listen_address: str = "127.0.0.1:0"
    validator_jsonrpc_listen_address: str = "127.0.0.1:0"
    storage_database_path: str | None = None

    def update(self, config_file):
        # Base config updates
        config_file['json_server_settings']['json_rpc_listen_address'] = self.json_rpc_listen_address
        config_file['grpc_settings']['grpc_listen_address'] = self.grpc_listen_address
        config_file['validator_settings']['validator_grpc_listen_address'] = self.validator_grpc_listen_address
        config_file['validator_settings']['validator_jsonrpc_listen_address'] = self.validator_jsonrpc_listen_address
        if self.storage_database_path is not None:
            config_file.setdefault('storage', {})
            config_file['storage'].setdefault('database', {})
            config_file['storage']['database']['path'] = self.storage_database_path

        return config_file
