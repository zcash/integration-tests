#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

"""Exercise current Zallet recovery against a fresh native Zinder runtime.

The runtime smoke includes the P2a chain-view expiry proofs and the P2b
transparent receive, spend, restart, and historical-rescan proof. Transaction
submission through Zinder remains outside this scenario.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import signal
import shutil
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import toml

from test_framework.config import ZebraArgs, ZalletArgs
from test_framework.proxy import JSONRPCException
from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import (
    COIN,
    COINBASE_MATURITY,
    INTERNAL_FEE,
    MIN_CONFIRMATIONS,
    Pool,
    PrivacyPolicy,
    Receiver,
    bitcoind_processes,
    first_transparent_receiver,
    get_rpc_auth_proxy,
    get_rpc_proxy,
    indexer_rpc_port,
    node_dir,
    p2p_port,
    prepare_wallets_for_mining,
    rpc_port,
    rpc_url,
    unified_address_for,
    update_zallet_conf,
    update_zebrad_conf,
    wait_and_assert_operationid_status,
    wait_for_account_spendable,
    wait_for_stable_mature_coinbase,
    wallet_dir,
)
from test_framework.zip317 import MINIMUM_FEE


NETWORK_NAME = "zcash-regtest"
INITIAL_HEIGHT = 3
READINESS_TIMEOUT_SECONDS = 240
PROCESS_STOP_TIMEOUT_SECONDS = 15
# Keep individual transport failures bounded while allowing normal wallet
# database and key operations to complete.
HTTP_TIMEOUT_SECONDS = 30
POLL_INTERVAL_SECONDS = 0.2
ZALLET_SCANNED_PRIORITY = 10
FULL_BLOCK_PAGE_SIZE = 1_000
STEADY_STATE_REGION_BLOCKS = 100
HISTORY_BLOCK_COUNT = (
    FULL_BLOCK_PAGE_SIZE + STEADY_STATE_REGION_BLOCKS + 1
)
STEADY_BLOCK_COUNT = FULL_BLOCK_PAGE_SIZE + 1
BLOCK_GENERATION_BATCH_SIZE = 100
PAIR_PUBLICATION_METRIC = "zinder_wallet_serving_pair_publisher_publications_total"
INGEST_CONTROL_ADMISSION_SETTLE_SECONDS = 3
TRANSPARENT_FUNDING_ZEC = Decimal("0.02")
TRANSPARENT_SPEND_ZEC = Decimal("0.01")
PRODUCER_COINBASE_DIVERSIFIER = 19
PRODUCER_SHIELDED_DIVERSIFIER = 20
TRANSPARENT_SOURCE_DIVERSIFIER = 21
TRANSPARENT_DESTINATION_DIVERSIFIER = 22
RANGE_BARRIER_DIRECTORY_ENVIRONMENT = "ZIT_RANGE_BARRIER_DIR"
RANGE_REQUEST_PAUSE_START_HEIGHT_ENVIRONMENT = (
    "ZIT_RANGE_REQUEST_PAUSE_START_HEIGHT"
)
RANGE_REQUEST_PAUSED_MARKER = "range-request-paused.json"
RANGE_REQUEST_CONTINUE_MARKER = "continue-range-request"
RANGE_MARKER_FIELDS = {
    "schema_version",
    "attempt_number",
    "requested_start_height_inclusive",
    "requested_end_height_inclusive",
    "chain_epoch_id",
}
ZINDER_REVISION = "df770c5ccb21bb8af4485a44d0815628388459e4"
ZALLET_REVISION = "143732c9ed5c04fb5a20440c4a162a2adc8bf899"
ZEBRA_REVISION = "7121c82ba6795a151523d5b308a19743f4a1ade7"
EVIDENCE_DIRECTORY_ENVIRONMENT = "ZIT_ZINDER_EVIDENCE_DIR"
CERTIFICATION_SLICE_ENVIRONMENT = "ZIT_ZALLET_CERTIFICATION_SLICE"

# This is the current Zallet P2 requirement constant.  The endpoint can
# advertise additive capabilities, but the launcher must admit this complete,
# ordered set before opening either wallet database for a live process.
ZALLET_REQUIRED_CAPABILITIES = (
    "wallet.read.server_info_v2",
    "wallet.read.network_upgrade_activations_v1",
    "wallet.read.visible_tip_block_v1",
    "wallet.read.block_id_by_selector_v1",
    "wallet.read.tree_state_at_height_v2",
    "wallet.read.subtree_roots_in_range_v1",
    "wallet.read.subtree_roots_ironwood_v1",
    "wallet.read.full_block_at_v1",
    "wallet.read.full_block_range_v1",
    "wallet.read.transaction_by_id_v2",
    "wallet.broadcast.transaction_v1",
    "wallet.address.transparent_unspent_outputs_v1",
    "wallet.address.transparent_history_v1",
    "wallet.events.chain_v1",
    "wallet.snapshot.mempool_v3",
    "wallet.events.mempool_v2",
)

REGTEST_NUPARAMS = (
    "5ba81b19:1", "76b809bb:1", "2bb40e60:1", "f5b9230b:1",
    "e9ff75a6:1", "c2d6d0b4:1", "c8e71055:1", "4dec4df0:1",
    "5437f330:1", "37a5165b:1",
)

ARTIFACTS = {
    "zebrad": ("ZEBRAD", "ZIT_ZEBRAD_SHA256"),
    "zinder_ingest": ("ZIT_ZINDER_INGEST", "ZIT_ZINDER_INGEST_SHA256"),
    "zinder_projector": ("ZIT_ZINDER_PROJECTOR", "ZIT_ZINDER_PROJECTOR_SHA256"),
    "zinder_query": ("ZIT_ZINDER_QUERY", "ZIT_ZINDER_QUERY_SHA256"),
    "zallet": ("ZIT_ZALLET_LAUNCHER", "ZIT_ZALLET_LAUNCHER_SHA256"),
    "zallet_zinder": ("ZIT_ZALLET_ZINDER", "ZIT_ZALLET_ZINDER_SHA256"),
    "zallet_zebra": ("ZIT_ZALLET_ZEBRA", "ZIT_ZALLET_ZEBRA_SHA256"),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def required_artifacts() -> tuple[dict[str, Path], dict[str, str]]:
    paths = {}
    hashes = {}
    for name, (path_variable, hash_variable) in ARTIFACTS.items():
        encoded_path = os.environ.get(path_variable)
        expected_hash = os.environ.get(hash_variable)
        if not encoded_path or not expected_hash:
            raise RuntimeError(
                "runtime smoke requires both {} and {}".format(
                    path_variable, hash_variable))
        path = Path(encoded_path)
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
            raise RuntimeError("{} must name an executable absolute file".format(path_variable))
        actual_hash = sha256_file(path)
        if actual_hash != expected_hash:
            raise RuntimeError(
                "{} sha256 differs: expected {}, got {}".format(
                    name, expected_hash, actual_hash))
        paths[name] = path
        hashes[name] = actual_hash
    for variable, expected in (("ZIT_ZINDER_REVISION", ZINDER_REVISION),
                               ("ZIT_ZALLET_REVISION", ZALLET_REVISION),
                               ("ZIT_ZEBRA_REVISION", ZEBRA_REVISION)):
        if os.environ.get(variable) != expected:
            raise RuntimeError("{} must equal {}".format(variable, expected))
    return paths, hashes


def require_evidence_directory() -> Path:
    encoded_path = os.environ.get(EVIDENCE_DIRECTORY_ENVIRONMENT)
    if not encoded_path:
        raise RuntimeError("runtime smoke requires {}".format(EVIDENCE_DIRECTORY_ENVIRONMENT))
    path = Path(encoded_path)
    if not path.is_absolute():
        raise RuntimeError("{} must be absolute".format(EVIDENCE_DIRECTORY_ENVIRONMENT))
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise RuntimeError("{} must name an empty directory".format(EVIDENCE_DIRECTORY_ENVIRONMENT))
    else:
        path.mkdir(parents=True)
    return path


def certification_slice() -> str:
    selected = os.environ.get(
        CERTIFICATION_SLICE_ENVIRONMENT,
        "complete",
    )
    if selected not in {"complete", "p2b", "p2c"}:
        raise RuntimeError(
            "{} must be complete, p2b, or p2c".format(
                CERTIFICATION_SLICE_ENVIRONMENT))
    return selected


def allocate_port(allocated: set[int]) -> int:
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        if port not in allocated:
            allocated.add(port)
            return port


def clean_zinder_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items()
            if not key.startswith("ZINDER_")}


def http_json(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def http_text(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return response.status, response.read().decode("utf8")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf8")


@dataclass
class RuntimePaths:
    root: Path
    canonical: Path
    wallet: Path
    configs: dict[str, Path]
    ops: dict[str, str]
    query_endpoint: str
    ingest_control_endpoint: str


class ManagedChildren:
    """Own the process logs needed to diagnose this one smoke run."""

    def __init__(self, root: Path, evidence: Path):
        self.root = root
        self.evidence = evidence
        self.children: dict[str, subprocess.Popen] = {}
        self.files: dict[str, tuple[object, object]] = {}
        self.paths: dict[str, tuple[Path, Path]] = {}

    def start(self, name: str, command: list[str], environment: dict[str, str]) -> subprocess.Popen:
        stdout_path = self.root / "logs" / (name + ".stdout.log")
        stderr_path = self.root / "logs" / (name + ".stderr.log")
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout = stdout_path.open("xb")
        try:
            stderr = stderr_path.open("xb")
            child = subprocess.Popen(command, env=environment, stdout=stdout, stderr=stderr)
        except Exception:
            stdout.close()
            if 'stderr' in locals():
                stderr.close()
            raise
        self.children[name] = child
        self.files[name] = (stdout, stderr)
        self.paths[name] = (stdout_path, stderr_path)
        return child

    def stop(self, name: str) -> int:
        child = self.children[name]
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
        stdout, stderr = self.files[name]
        stdout.close()
        stderr.close()
        for source in self.paths[name]:
            destination = self.evidence / "logs" / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        return child.returncode

    def stop_all(self) -> dict[str, int]:
        exits = {}
        for name in reversed(list(self.children)):
            if name in self.files:
                exits[name] = self.stop(name)
                del self.files[name]
        return exits


class WalletZinderRecoverySmoke(BitcoinTestFramework):
    def __init__(self):
        super().__init__()
        self.num_nodes = 1
        self.num_indexers = 0
        self.num_wallets = 0
        self.cache_behavior = "clean"
        self.activation_heights = {"NU5": 1, "NU6": 1, "NU6.1": 1, "NU6.2": 1, "NU6.3": 1}
        self.artifacts: dict[str, Path] = {}
        self.artifact_hashes: dict[str, str] = {}
        self.evidence: Path | None = None
        self.runtime: RuntimePaths | None = None
        self.children: ManagedChildren | None = None
        self.allocated_ports: set[int] = set()
        self.zebra_process: subprocess.Popen | None = None
        self.zebra_logs: tuple[Path, Path] | None = None
        self.zebra_files: tuple[object, object] | None = None
        self.zebra_health_endpoint = ""
        self.suspended_backend_pids: set[int] = set()
        self.producer_config: Path | None = None
        self.bulk_miner_address = ""
        self.seed_coinbase_block_hash = ""
        self.seed_coinbase_tip = 0
        self.producer_seedfp = ""
        self.producer_account_uuid = ""
        self.producer_coinbase_address = ""
        self.producer_shielded_address = ""
        self.transparent_source_address = ""
        self.transparent_seed_snapshot: Path | None = None

    def setup_chain(self):
        super().setup_chain()
        self.artifacts, self.artifact_hashes = required_artifacts()
        self.evidence = require_evidence_directory()
        self._record_artifacts()
        self.allocated_ports.update(
            (rpc_port(0), p2p_port(0), indexer_rpc_port(0)))
        producer_root = Path(self.options.tmpdir) / "transparent-producer"
        previous_backend = os.environ.get("ZALLET_BACKEND")
        os.environ["ZALLET_BACKEND"] = "zebra"
        try:
            producer_launcher = self._staged_producer_launcher()
            miner_addresses = prepare_wallets_for_mining(
                2,
                str(producer_root),
                binary=[producer_launcher, producer_launcher],
                zallet_args=[
                    ZalletArgs(activation_heights=self.activation_heights),
                    ZalletArgs(activation_heights=self.activation_heights),
                ],
            )
            self.bulk_miner_address = miner_addresses[1].strip()
        finally:
            if previous_backend is None:
                del os.environ["ZALLET_BACKEND"]
            else:
                os.environ["ZALLET_BACKEND"] = previous_backend
        self.producer_config = Path(
            wallet_dir(str(producer_root), 0)) / "zallet.toml"
        producer_config = toml.load(self.producer_config)
        producer_config["indexer"]["validator_address"] = (
            "127.0.0.1:{}".format(rpc_port(0)))
        producer_config["indexer"]["read_state_service"] = {
            "grpc_address": "127.0.0.1:{}".format(indexer_rpc_port(0)),
            "zebra_state_path": str(
                Path(node_dir(self.options.tmpdir, 0)).resolve()),
        }
        self.producer_config.write_text(
            toml.dumps(producer_config),
            encoding="utf8",
        )
        self.allocated_ports.add(int(
            producer_config["rpc"]["bind"][0].rsplit(":", 1)[1]))

    def setup_nodes(self):
        self.allocated_ports.update((rpc_port(0), p2p_port(0), indexer_rpc_port(0)))
        health_port = allocate_port(self.allocated_ports)
        health_address = "127.0.0.1:{}".format(health_port)
        data_directory = Path(node_dir(self.options.tmpdir, 0))
        config_path = Path(update_zebrad_conf(
            str(data_directory), rpc_port(0), p2p_port(0), indexer_rpc_port(0),
            ZebraArgs(
                miner_address=self.bulk_miner_address,
                activation_heights=self.activation_heights)))
        config = toml.load(config_path)
        config["health"] = {"listen_addr": health_address, "enforce_on_test_networks": False}
        with config_path.open("w", encoding="utf8") as destination:
            toml.dump(config, destination)
        assert self.evidence is not None
        shutil.copyfile(config_path, self.evidence / "zebra-config.toml")
        stdout_path = Path(self.options.tmpdir) / "zebra.stdout.log"
        stderr_path = Path(self.options.tmpdir) / "zebra.stderr.log"
        stdout = stdout_path.open("xb")
        stderr = stderr_path.open("xb")
        self.zebra_logs = (stdout_path, stderr_path)
        self.zebra_files = (stdout, stderr)
        self.zebra_process = subprocess.Popen(
            [self.artifacts["zebrad"], "-c={}".format(config_path), "start"],
            stdout=stdout, stderr=stderr)
        bitcoind_processes[0] = self.zebra_process
        endpoint = rpc_url(0)
        self.zebra_health_endpoint = "http://{}/ready".format(health_address)
        deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
        latest_error = "endpoint has not responded"
        while time.monotonic() < deadline:
            if self.zebra_process.poll() is not None:
                raise RuntimeError("Zebra exited before RPC readiness")
            try:
                node = get_rpc_proxy(endpoint, 0, timeout=HTTP_TIMEOUT_SECONDS)
                node.getblockcount()
                status, body = http_text(self.zebra_health_endpoint)
                if status == 200 and body == "ok":
                    return [node]
            except (OSError, ValueError, urllib.error.URLError, JSONRPCException) as error:
                latest_error = str(error)
            time.sleep(POLL_INTERVAL_SECONDS)
        raise RuntimeError("Zebra did not become ready: {}".format(latest_error))

    def prepare_chain(self):
        self.nodes[0].generate(INITIAL_HEIGHT)
        if self.nodes[0].getblockcount() != INITIAL_HEIGHT:
            raise RuntimeError("fresh Zebra did not reach initial height")

    def run_test(self):
        failure = None
        try:
            self._run_smoke()
        except Exception as error:
            failure = error
        cleanup_error = self._cleanup()
        self._write_manifest(failure, cleanup_error)
        if failure is not None:
            raise failure
        if cleanup_error is not None:
            raise cleanup_error

    def _run_smoke(self):
        assert self.evidence is not None
        runtime_root = Path(self.options.tmpdir) / "zinder-runtime"
        self.runtime = self._render_runtime(runtime_root)
        self.children = ManagedChildren(runtime_root, self.evidence)
        for service in ("ingest", "projector", "query"):
            child = self.children.start(
                "zinder-" + service,
                [str(self.artifacts["zinder_" + service]), "--config", str(self.runtime.configs[service])],
                clean_zinder_environment())
            self._wait_ready(service, child, INITIAL_HEIGHT)
        query_health = self._health("query")
        capabilities = query_health.get("capabilities")
        if not isinstance(capabilities, list) or not all(isinstance(value, str) for value in capabilities):
            raise RuntimeError("zinder-query did not report string capabilities")
        missing = [value for value in ZALLET_REQUIRED_CAPABILITIES if value not in capabilities]
        if missing:
            raise RuntimeError("Zinder endpoint misses required Zallet capabilities: {}".format(missing))
        self._write_json("capability-admission.json", {
            "required_capabilities": list(ZALLET_REQUIRED_CAPABILITIES),
            "advertised_capabilities": capabilities,
        })
        self._prepare_transparent_seed_snapshot()
        p2a_starting_tip = self.seed_coinbase_tip
        selected_slice = certification_slice()
        if selected_slice == "p2b":
            transparent_recovery = self._run_transparent_recovery()
            self._write_json("smoke-result.json", {
                "initial_height": INITIAL_HEIGHT,
                "p2b_starting_height": p2a_starting_tip,
                "transparent_recovery": transparent_recovery,
                "certification_slice": selected_slice,
                "scope": "P2b transparent recovery",
            })
            return
        if selected_slice == "p2c":
            transparent_recovery = self._run_transparent_recovery(
                submit_through_zinder=True,
            )
            self._write_json("smoke-result.json", {
                "initial_height": INITIAL_HEIGHT,
                "p2c_starting_height": p2a_starting_tip,
                "transparent_recovery": transparent_recovery,
                "certification_slice": selected_slice,
                "scope": "P2b fixture plus P2c submit lifecycle",
            })
            return

        source_config = self._write_wallet_config("source")
        watcher_config = self._write_wallet_config("watcher")
        source_seedfp = self._initialize_wallet("source", source_config, generate_mnemonic=True)
        self._initialize_wallet("watcher", watcher_config, generate_mnemonic=False)
        source = self._start_wallet("source", source_config)
        watcher = self._start_wallet("watcher", watcher_config)
        self._wait_wallet_tip(source, p2a_starting_tip)
        self._wait_wallet_tip(watcher, p2a_starting_tip)

        if source.z_listaccounts(False):
            raise RuntimeError("source wallet unexpectedly contained an offline-created account")
        if watcher.z_listaccounts(False):
            raise RuntimeError("watcher wallet unexpectedly contained an offline-created account")
        live_accounts = self._create_live_accounts(source, source_seedfp)
        self._wait_wallet_fully_synced(source, p2a_starting_tip)
        viewing_key = live_accounts[0]["viewing_key"]
        try:
            imported = watcher.z_importviewingkey(viewing_key, "yes", 1)
        except Exception as error:
            raise RuntimeError(
                "native Zinder runtime did not complete non-genesis viewing-key "
                "import: {}".format(error)) from error
        if imported.get("address_type") != "sapling":
            raise RuntimeError("viewing-key import did not create a Sapling watch-only address")
        self._wait_wallet_fully_synced(watcher, p2a_starting_tip)

        known_block_hash = self.nodes[0].getblockhash(1)
        known_transaction = self.nodes[0].getblock(known_block_hash)["tx"][0]
        transaction = source.getrawtransaction(known_transaction, 1, known_block_hash)
        if transaction.get("blockhash") != known_block_hash or not transaction.get("hex"):
            raise RuntimeError("Zallet did not return the known mined coinbase through Zinder")

        previous_tip = self.nodes[0].getblockcount()
        self.nodes[0].generate(1)
        advanced_tip = self.nodes[0].getblockcount()
        if advanced_tip != previous_tip + 1:
            raise RuntimeError("Zebra did not advance one block for quiet mempool follow")
        for service in ("ingest", "projector", "query"):
            self._wait_ready(service, self.children.children["zinder-" + service], advanced_tip)
        self._wait_wallet_fully_synced(source, advanced_tip)
        self._wait_wallet_fully_synced(watcher, advanced_tip)
        self._wait_for_backend_trace(
            "zallet-source",
            "ending Zinder mempool follow after visible-tip transition")
        # Both wallet tips after the backend has reported its quiet
        # mempool-follow termination are the reacquired view at the mined tip.
        source_status = source.getwalletstatus()
        watcher_status = watcher.getwalletstatus()
        accounts_after_advance = self._assert_live_accounts(source, live_accounts)
        source.stop()
        source_exit = self.children.stop("zallet-source")
        if source_exit != 0:
            raise RuntimeError("source Zallet did not stop cleanly: {}".format(source_exit))
        source = self._start_wallet("source-restarted", source_config)
        self._wait_wallet_fully_synced(source, advanced_tip)
        accounts_after_restart = self._assert_live_accounts(source, live_accounts)

        rescan = watcher.z_importviewingkey(viewing_key, "yes", 1)
        if rescan.get("address_type") != "sapling":
            raise RuntimeError("existing viewing-key rescan did not return a Sapling address")
        self._wait_wallet_fully_synced(watcher, advanced_tip)
        expiry_recovery = self._run_expiry_recovery(
            source,
            watcher,
            viewing_key,
            advanced_tip,
        )
        transparent_recovery = self._run_transparent_recovery(
            submit_through_zinder=True,
        )
        self._write_json("smoke-result.json", {
            "initial_height": INITIAL_HEIGHT,
            "p2a_starting_height": p2a_starting_tip,
            "advanced_height": advanced_tip,
            "known_coinbase_transaction_id": known_transaction,
            "quiet_mempool_follow": {
                "backend_trace": "ending Zinder mempool follow after visible-tip transition",
                "source_wallet_tip_after_reacquisition": source_status.get("wallet_tip"),
                "watcher_wallet_tip_after_reacquisition": watcher_status.get("wallet_tip"),
            },
            "existing_viewing_key_rescan": (
                "RPC accepted rescan=yes for the existing account and the "
                "wallet returned to fully synced"),
            "live_account_creation": {
                "creation_height": p2a_starting_tip,
                "accounts": [
                    {
                        "account_uuid": account["account_uuid"],
                        "name": account["name"],
                        "seedfp": account["seedfp"],
                        "zip32_account_index": account["zip32_account_index"],
                        "sapling_address": account["sapling_address"],
                        "viewing_key_sha256": hashlib.sha256(
                            account["viewing_key"].encode("utf8")).hexdigest(),
                    }
                    for account in live_accounts
                ],
                "accounts_after_advance": accounts_after_advance,
                "accounts_after_restart": accounts_after_restart,
            },
            "expiry_recovery": expiry_recovery,
            "transparent_recovery": transparent_recovery,
            "certification_slice": selected_slice,
            "scope": "P2a expiry recovery plus P2b transparent recovery",
        })

    def _prepare_transparent_seed_snapshot(self):
        assert self.runtime is not None and self.children is not None
        assert self.producer_config is not None and self.evidence is not None
        producer = self._start_wallet(
            "transparent-producer-seed",
            self.producer_config,
            launcher=self._staged_producer_launcher(),
        )
        self._wait_wallet_fully_synced(producer, INITIAL_HEIGHT)
        accounts = producer.z_listaccounts(False)
        if len(accounts) != 1:
            raise RuntimeError(
                "producer wallet did not contain exactly its regtest account")
        self.producer_account_uuid = accounts[0]["account_uuid"]
        self.producer_seedfp = accounts[0].get("seedfp", "")
        if not self.producer_seedfp.startswith("zip32seedfp1"):
            raise RuntimeError(
                "producer wallet account did not report its seed fingerprint")
        self.producer_shielded_address = unified_address_for(
            producer,
            self.producer_account_uuid,
            PRODUCER_SHIELDED_DIVERSIFIER,
            receivers=(Receiver.ORCHARD,),
        )
        self.producer_coinbase_address = first_transparent_receiver(
            producer,
            unified_address_for(
                producer,
                self.producer_account_uuid,
                PRODUCER_COINBASE_DIVERSIFIER,
            ),
        )
        source_unified_address = unified_address_for(
            producer,
            self.producer_account_uuid,
            TRANSPARENT_SOURCE_DIVERSIFIER,
        )
        self.transparent_source_address = first_transparent_receiver(
            producer,
            source_unified_address,
        )
        seed_coinbase_blocks = self.nodes[0].generatetoaddress(
            1,
            self.producer_coinbase_address,
        )
        if len(seed_coinbase_blocks) != 1:
            raise RuntimeError(
                "Zebra did not mine the producer seed coinbase")
        self.seed_coinbase_block_hash = seed_coinbase_blocks[0]
        self.seed_coinbase_tip = self.nodes[0].getblockcount()
        if self.seed_coinbase_tip != INITIAL_HEIGHT + 1:
            raise RuntimeError(
                "producer seed coinbase did not advance the initial tip")
        self._wait_zinder_tip(self.seed_coinbase_tip)
        self._wait_wallet_fully_synced(
            producer,
            self.seed_coinbase_tip,
        )
        self._stop_wallet(
            producer,
            "zallet-transparent-producer-seed",
        )

        self.transparent_seed_snapshot = (
            self.runtime.root / "transparent-seed-snapshot"
        )

    def _run_transparent_recovery(
            self,
            submit_through_zinder: bool = False) -> dict:
        assert self.runtime is not None and self.children is not None
        assert self.producer_config is not None
        assert self.transparent_seed_snapshot is not None
        current_tip = self.nodes[0].getblockcount()
        warm_producer = self._start_wallet(
            "transparent-producer-warm",
            self.producer_config,
            launcher=self._staged_producer_launcher(),
        )
        self._wait_wallet_fully_synced(warm_producer, current_tip)
        current_confirmations = (
            current_tip - self.seed_coinbase_tip + 1
        )
        maturity_and_data_barrier = max(
            1,
            COINBASE_MATURITY - current_confirmations + 1,
        )
        self._mine_blocks(maturity_and_data_barrier)
        pre_p2b_tip = self.nodes[0].getblockcount()
        self._wait_zinder_tip(pre_p2b_tip)
        self._wait_wallet_fully_synced(warm_producer, pre_p2b_tip)
        mature_coinbase = wait_for_stable_mature_coinbase(
            warm_producer,
            min_count=1,
            timeout=READINESS_TIMEOUT_SECONDS,
        )
        if (len(mature_coinbase) != 1
                or mature_coinbase[0].get("address")
                != self.producer_coinbase_address):
            raise RuntimeError(
                "warmed producer did not expose exactly its dedicated "
                "mature coinbase")
        warm_blocks = self._scanned_blocks(self.producer_config.parent)
        warm_heights = [height for height, _ in warm_blocks]
        if warm_heights != list(range(1, pre_p2b_tip + 1)):
            raise RuntimeError(
                "warmed producer did not scan a contiguous pre-P2b chain")
        if warm_blocks[-1][1] != self.nodes[0].getblockhash(pre_p2b_tip):
            raise RuntimeError(
                "warmed producer tip hash differed from Zebra")
        self._stop_wallet(
            warm_producer,
            "zallet-transparent-producer-warm",
        )
        shutil.copytree(
            self.producer_config.parent,
            self.transparent_seed_snapshot,
        )
        self._write_json("transparent-seed-snapshot.json", {
            "height": pre_p2b_tip,
            "account_uuid": self.producer_account_uuid,
            "coinbase_address": self.producer_coinbase_address,
            "coinbase_block_hash": self.seed_coinbase_block_hash,
            "coinbase_mined_tip": self.seed_coinbase_tip,
            "source_address": self.transparent_source_address,
            "scanned_blocks": self._scanned_block_summary(warm_blocks),
            "producer_wallet_writes_isolated": True,
        })

        producer = self._start_wallet(
            "transparent-producer",
            self.producer_config,
            launcher=self._staged_producer_launcher(),
        )
        self._wait_wallet_fully_synced(producer, pre_p2b_tip)
        producer_backend_pid = self._staged_backend_pid(
            "zallet-transparent-producer",
            self._staged_producer_backend(),
        )
        target_config = self._write_wallet_config(
            "transparent-target",
            clone_from=self.transparent_seed_snapshot,
        )
        target = self._start_wallet("transparent-target", target_config)
        target_process_name = "zallet-transparent-target"
        self._wait_wallet_fully_synced(target, pre_p2b_tip)
        target_backend_pid = self._staged_backend_pid(
            "zallet-transparent-target")
        if self._source_unspent_outputs(target):
            raise RuntimeError(
                "transparent source address was funded before the P2b send")
        starting_tip = pre_p2b_tip
        coinbase_confirmations = (
            pre_p2b_tip - self.seed_coinbase_tip + 1
        )
        if coinbase_confirmations < COINBASE_MATURITY:
            raise RuntimeError("producer seed coinbase was not mature")
        shielding = producer.z_shieldcoinbase(
            self.producer_coinbase_address,
            self.producer_shielded_address,
            None,
            1,
            None,
            PrivacyPolicy.ALLOW_REVEALED_SENDERS,
        )
        if shielding.get("shieldingUTXOs") != 1:
            raise RuntimeError(
                "producer did not select exactly one matured seed coinbase")
        if (shielding.get("remainingUTXOs") != 0
                or Decimal(shielding["remainingValue"]) != 0):
            raise RuntimeError(
                "producer left part of the dedicated seed coinbase unshielded")
        shielding_value_zec = Decimal(shielding["shieldingValue"])
        minimum_funding_zat = (
            int(TRANSPARENT_FUNDING_ZEC * COIN) + MINIMUM_FEE
        )
        if int(shielding_value_zec * COIN) < minimum_funding_zat:
            raise RuntimeError(
                "producer did not select enough shielding value for P2b")
        shielding_txid = wait_and_assert_operationid_status(
            producer,
            shielding["opid"],
        )
        if shielding_txid is None:
            raise RuntimeError("producer did not return a shielding transaction")
        shielding_mined_tip = self._mine_transaction_and_follow(
            shielding_txid,
            producer,
            target,
        )
        shielding_tip = self._mine_confirmation_and_follow(producer, target)
        self._wait_transaction_mined(
            target,
            shielding_txid,
            shielding_mined_tip,
        )
        ironwood_spendable_zat = wait_for_account_spendable(
            producer,
            self.producer_account_uuid,
            Pool.IRONWOOD,
            min_zat=minimum_funding_zat,
        )
        target_ironwood_spendable_zat = wait_for_account_spendable(
            target,
            self.producer_account_uuid,
            Pool.IRONWOOD,
            min_zat=ironwood_spendable_zat,
        )
        if target_ironwood_spendable_zat != ironwood_spendable_zat:
            raise RuntimeError(
                "Zinder-backed Zallet reported the wrong Ironwood balance")
        if self._source_unspent_outputs(target):
            raise RuntimeError(
                "shielding unexpectedly funded the P2b source address")
        destination_account = producer.z_getnewaccount(
            "Zinder P2b external destination",
            self.producer_seedfp,
        )["account_uuid"]
        destination_address = first_transparent_receiver(
            producer,
            unified_address_for(
                producer,
                destination_account,
                TRANSPARENT_DESTINATION_DIVERSIFIER,
            ),
        )
        self._wait_wallet_fully_synced(producer, shielding_tip)
        ironwood_selection_ready = (
            self._wait_ironwood_note_selection_ready(
                minimum_funding_zat,
                shielding_tip,
            )
        )

        funding_opid = producer.z_sendmany(
            self.producer_shielded_address,
            [{
                "address": self.transparent_source_address,
                "amount": TRANSPARENT_FUNDING_ZEC,
            }],
            MIN_CONFIRMATIONS,
            INTERNAL_FEE,
            PrivacyPolicy.ALLOW_REVEALED_RECIPIENTS,
        )
        funding_txid = wait_and_assert_operationid_status(
            producer,
            funding_opid,
        )
        if funding_txid is None:
            raise RuntimeError("producer did not return a funding transaction")
        funding_mined_tip = self._mine_transaction_and_follow(
            funding_txid,
            producer,
            target,
        )
        funding_tip = self._mine_confirmation_and_follow(producer, target)
        self._wait_transaction_mined(
            target,
            funding_txid,
            funding_mined_tip,
        )
        producer_funded_state = self._wait_transparent_state(
            producer,
            expected_source_utxos=1,
            expected_txids=(funding_txid,),
        )
        funded_state = self._wait_transparent_state(
            target,
            expected_source_utxos=1,
            expected_txids=(funding_txid,),
            expected_state=producer_funded_state,
        )
        funded_utxo = funded_state["source_utxos"][0]
        if (funded_utxo["txid"] != funding_txid
                or funded_utxo["valueZat"]
                != int(TRANSPARENT_FUNDING_ZEC * COIN)):
            raise RuntimeError(
                "Zinder-backed Zallet reported the wrong funded source UTXO")

        spending_wallet = target if submit_through_zinder else producer
        spend_opid = spending_wallet.z_sendmany(
            self.transparent_source_address,
            [{
                "address": destination_address,
                "amount": TRANSPARENT_SPEND_ZEC,
            }],
            MIN_CONFIRMATIONS,
            INTERNAL_FEE,
            PrivacyPolicy.ALLOW_FULLY_TRANSPARENT,
        )
        spend_txid = wait_and_assert_operationid_status(
            spending_wallet,
            spend_opid,
        )
        if spend_txid is None:
            raise RuntimeError("spending wallet did not return a transaction")
        submit_lifecycle = None
        if submit_through_zinder:
            self._wait_node_mempool(spend_txid)
            unmined_before_restart = self._wait_transaction_entry(
                target,
                spend_txid,
                mined_height=None,
            )
            self._wait_for_backend_trace(
                target_process_name,
                "Scanning mempool tx {}".format(spend_txid),
            )
            self._stop_wallet(target, target_process_name)
            target_process_name = (
                "zallet-transparent-target-mempool-restarted"
            )
            target = self._start_wallet(
                "transparent-target-mempool-restarted",
                target_config,
            )
            self._wait_wallet_fully_synced(target, funding_tip)
            target_backend_pid_after_mempool_restart = (
                self._staged_backend_pid(
                    target_process_name,
                )
            )
            self._wait_for_backend_trace(
                target_process_name,
                "Scanning mempool tx {}".format(spend_txid),
            )
            unmined_after_restart = self._wait_transaction_entry(
                target,
                spend_txid,
                mined_height=None,
            )
            submit_lifecycle = {
                "transaction_id": spend_txid,
                "submitted_backend": "zallet-zinder",
                "node_mempool_observed": True,
                "unmined_before_restart": unmined_before_restart,
                "unmined_after_restart": unmined_after_restart,
                "backend_pid_before_restart": target_backend_pid,
                "backend_pid_after_restart": (
                    target_backend_pid_after_mempool_restart
                ),
            }
        spend_mined_tip = self._mine_transaction_and_follow(
            spend_txid,
            producer,
            target,
        )
        spend_tip = self._mine_confirmation_and_follow(producer, target)
        self._wait_transaction_mined(
            target,
            spend_txid,
            spend_mined_tip,
        )
        if submit_lifecycle is not None:
            submit_lifecycle["mined"] = self._wait_transaction_entry(
                target,
                spend_txid,
                mined_height=spend_mined_tip,
            )
            submit_lifecycle["shallow_reorg"] = (
                self._exercise_shallow_reorg(
                    spend_txid,
                    self.nodes[0].getblockhash(spend_mined_tip),
                    spend_mined_tip,
                    producer,
                    target,
                    target_config,
                )
            )
        expected_txids = (funding_txid, spend_txid)
        producer_final_state = self._wait_transparent_state(
            producer,
            expected_source_utxos=0,
            expected_txids=expected_txids,
        )
        final_state = self._wait_transparent_state(
            target,
            expected_source_utxos=0,
            expected_txids=expected_txids,
            expected_state=producer_final_state,
        )

        self._stop_wallet(target, target_process_name)
        restarted = self._start_wallet(
            "transparent-target-restarted",
            target_config,
        )
        self._wait_wallet_fully_synced(restarted, spend_tip)
        restarted_state = self._wait_transparent_state(
            restarted,
            expected_source_utxos=0,
            expected_txids=expected_txids,
            expected_state=final_state,
        )
        self._stop_wallet(
            restarted,
            "zallet-transparent-target-restarted",
        )

        rescan_config = self._write_wallet_config(
            "transparent-rescan",
            clone_from=self.transparent_seed_snapshot,
        )
        rescanned = self._start_wallet(
            "transparent-rescan",
            rescan_config,
        )
        self._wait_wallet_fully_synced(rescanned, spend_tip)
        rescan_state = self._wait_transparent_rescan_recovery(
            rescanned,
            expected_txids=expected_txids,
            expected_state=final_state,
        )
        rescan_backend_pid = self._staged_backend_pid(
            "zallet-transparent-rescan")

        target_stage = self._staged_target_files()
        producer_stage = self._staged_producer_files()
        result = {
            "pre_p2b_tip": pre_p2b_tip,
            "starting_tip": starting_tip,
            "mature_coinbase": {
                "block_count": 1,
                "block_hash": self.seed_coinbase_block_hash,
                "mined_tip": self.seed_coinbase_tip,
                "address": self.producer_coinbase_address,
                "confirmations_at_shielding": coinbase_confirmations,
            },
            "coinbase_shielding": {
                "transaction_id": shielding_txid,
                "mined_tip": shielding_mined_tip,
                "tip": shielding_tip,
                "destination_address": self.producer_shielded_address,
                "selected_utxos": shielding["shieldingUTXOs"],
                "remaining_utxos": shielding["remainingUTXOs"],
                "remaining_value_zec": str(shielding["remainingValue"]),
                "selected_value_zec": str(shielding_value_zec),
                "selection_ready": ironwood_selection_ready,
                "producer_spendable_value_zat": ironwood_spendable_zat,
                "target_spendable_value_zat": target_ironwood_spendable_zat,
                "target_matches_producer": (
                    target_ironwood_spendable_zat == ironwood_spendable_zat
                ),
            },
            "funding_mined_tip": funding_mined_tip,
            "funding_confirmed_tip": funding_tip,
            "spend_mined_tip": spend_mined_tip,
            "spend_confirmed_tip": spend_tip,
            "source_address": self.transparent_source_address,
            "destination_address": destination_address,
            "funding_transaction_id": funding_txid,
            "spend_transaction_id": spend_txid,
            "submit_lifecycle": submit_lifecycle,
            "producer_funded_state": producer_funded_state,
            "funded_state": funded_state,
            "funded_state_matches_producer": (
                funded_state == producer_funded_state
            ),
            "producer_final_state": producer_final_state,
            "final_state": final_state,
            "final_state_matches_producer": (
                final_state == producer_final_state
            ),
            "restart_state_equal": restarted_state == final_state,
            "historical_rescan_spendable_state_equal": (
                rescan_state["transparent_balance"]
                == final_state["transparent_balance"]
                and rescan_state["source_utxos"]
                == final_state["source_utxos"]
            ),
            "historical_rescan_mined_history_equal": (
                self._mined_history(rescan_state)
                == self._mined_history(final_state)
            ),
            "historical_rescan_account_deltas_equal": (
                rescan_state["history"] == final_state["history"]
            ),
            "historical_rescan_sent_output_gap": (
                "https://github.com/zcash/zallet/issues/698"
            ),
            "processes": {
                "producer_backend_pid": producer_backend_pid,
                "target_backend_pid": target_backend_pid,
                "rescan_backend_pid": rescan_backend_pid,
            },
            "backend_isolation": {
                "target_stage_files": target_stage,
                "producer_stage_files": producer_stage,
                "target_has_bundled_endpoint_fallback": False,
                "producer_wallet_writes_shared_with_targets": False,
            },
        }
        self._write_json("transparent-recovery.json", result)
        return result

    def _mine_transaction_and_follow(
            self,
            transaction_id: str,
            producer,
            target) -> int:
        self._wait_node_mempool(transaction_id)
        mined = self.nodes[0].generate(1)
        if len(mined) != 1:
            raise RuntimeError("Zebra did not mine one P2b transaction block")
        if transaction_id not in self.nodes[0].getblock(mined[0])["tx"]:
            raise RuntimeError(
                "Zebra did not include the P2b transaction in the mined block")
        tip = self.nodes[0].getblockcount()
        self._wait_zinder_tip(tip)
        self._wait_wallet_fully_synced(producer, tip)
        self._wait_wallet_fully_synced(target, tip)
        self._wait_transaction_mined(producer, transaction_id, tip)
        return tip

    def _wait_node_mempool(self, transaction_id: str):
        deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if transaction_id in self.nodes[0].getrawmempool():
                return
            time.sleep(POLL_INTERVAL_SECONDS)
        raise RuntimeError(
            "Zebra did not admit the wallet transaction to its mempool")

    def _wait_transaction_entry(
            self,
            wallet,
            transaction_id: str,
            mined_height: int | None) -> dict:
        deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                matches = [
                    transaction
                    for transaction in wallet.z_listtransactions(
                        self.producer_account_uuid,
                    )
                    if transaction["txid"] == transaction_id
                    and transaction.get("mined_height") == mined_height
                ]
                if len(matches) == 1:
                    transaction = matches[0]
                    balance_delta = transaction.get(
                        "account_balance_delta")
                    if not isinstance(balance_delta, int):
                        raise RuntimeError(
                            "wallet transaction omitted its account delta")
                    if balance_delta >= 0:
                        raise RuntimeError(
                            "wallet transaction was not classified as outgoing")
                    return {
                        "txid": transaction_id,
                        "mined_height": mined_height,
                        "account_balance_delta": balance_delta,
                    }
            except (OSError, JSONRPCException):
                pass
            time.sleep(POLL_INTERVAL_SECONDS)
        raise RuntimeError(
            "wallet transaction did not reach mined height {}".format(
                mined_height))

    def _exercise_shallow_reorg(
            self,
            transaction_id: str,
            original_block_hash: str,
            original_mined_height: int,
            producer,
            target,
            target_config: Path) -> dict:
        self.nodes[0].invalidateblock(original_block_hash)
        self._wait_node_mempool(transaction_id)
        replacement_blocks = self.nodes[0].generate(2)
        if len(replacement_blocks) != 2:
            raise RuntimeError(
                "Zebra did not mine the two-block replacement branch")
        replacement_block_hash = self.nodes[0].getblockhash(
            original_mined_height)
        if replacement_block_hash == original_block_hash:
            raise RuntimeError(
                "Zebra replacement retained the invalidated block")
        if transaction_id not in self.nodes[0].getblock(
                replacement_block_hash)["tx"]:
            raise RuntimeError(
                "replacement block did not remine the wallet transaction")
        replacement_tip = original_mined_height + 1
        if self.nodes[0].getblockcount() != replacement_tip:
            raise RuntimeError("Zebra replacement branch has the wrong tip")
        self._wait_zinder_tip(replacement_tip)
        self._wait_wallet_fully_synced(producer, replacement_tip)
        self._wait_wallet_fully_synced(target, replacement_tip)
        self._wait_transaction_mined(
            producer,
            transaction_id,
            original_mined_height,
        )
        target_scanned_blocks = self._wait_scanned_replacement(
            target_config,
            original_mined_height,
            replacement_blocks,
            original_block_hash,
        )
        replacement_entry = self._wait_transaction_entry(
            target,
            transaction_id,
            mined_height=original_mined_height,
        )
        transaction = target.getrawtransaction(
            transaction_id,
            1,
            replacement_block_hash,
        )
        if transaction.get("blockhash") != replacement_block_hash:
            raise RuntimeError(
                "Zallet retained stale block identity after the reorg")
        return {
            "original_block_hash": original_block_hash,
            "replacement_block_hash": replacement_block_hash,
            "replacement_tip": replacement_tip,
            "replacement_transaction": replacement_entry,
            "target_scanned_blocks": target_scanned_blocks,
            "stale_block_identity_absent": True,
        }

    def _wait_scanned_replacement(
            self,
            wallet_config: Path,
            replacement_start_height: int,
            replacement_blocks: list[str],
            invalidated_block_hash: str) -> list[dict]:
        deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
        expected = {
            replacement_start_height + offset: block_hash
            for offset, block_hash in enumerate(replacement_blocks)
        }
        while time.monotonic() < deadline:
            scanned = dict(self._scanned_blocks(wallet_config.parent))
            if (
                all(scanned.get(height) == block_hash
                    for height, block_hash in expected.items())
                and invalidated_block_hash not in scanned.values()
            ):
                return [
                    {"height": height, "hash": block_hash}
                    for height, block_hash in expected.items()
                ]
            time.sleep(POLL_INTERVAL_SECONDS)
        raise RuntimeError(
            "wallet database did not replace the invalidated branch")

    def _mine_confirmation_and_follow(self, producer, target) -> int:
        mined = self.nodes[0].generate(1)
        if len(mined) != 1:
            raise RuntimeError(
                "Zebra did not mine the P2b confirmation block")
        tip = self.nodes[0].getblockcount()
        self._wait_zinder_tip(tip)
        self._wait_wallet_fully_synced(producer, tip)
        self._wait_wallet_fully_synced(target, tip)
        return tip

    def _wait_transaction_mined(
            self,
            wallet,
            transaction_id: str,
            mined_height: int):
        deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                transactions = wallet.z_listtransactions(
                    self.producer_account_uuid)
                matching = [
                    transaction
                    for transaction in transactions
                    if transaction["txid"] == transaction_id
                ]
                if (len(matching) == 1
                        and matching[0].get("mined_height") == mined_height):
                    return
            except (OSError, JSONRPCException):
                pass
            time.sleep(POLL_INTERVAL_SECONDS)
        raise RuntimeError(
            "wallet did not record the P2b transaction at its mined height")

    def _wait_ironwood_note_selection_ready(
            self,
            minimum_value_zat: int,
            anchor_height: int) -> dict:
        assert self.producer_config is not None
        wallet_database = self.producer_config.parent / "wallet.db"
        account_uuid = uuid.UUID(self.producer_account_uuid).bytes
        deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                connection = sqlite3.connect(
                    "file:{}?mode=ro".format(wallet_database),
                    uri=True,
                )
                try:
                    row = connection.execute(
                        """
                        SELECT
                            note.id,
                            transaction_record.block,
                            note.value,
                            note.commitment_tree_position,
                            shard.shard_index,
                            shard.max_priority
                        FROM ironwood_received_notes AS note
                        INNER JOIN transactions AS transaction_record
                            ON transaction_record.id_tx = note.transaction_id
                        INNER JOIN accounts AS account
                            ON account.id = note.account_id
                        INNER JOIN v_ironwood_shards_scan_state AS shard
                            ON note.commitment_tree_position
                                >= shard.start_position
                            AND note.commitment_tree_position
                                < shard.end_position_exclusive
                        WHERE account.uuid = ?
                            AND account.has_spend_key = 1
                            AND account.ufvk IS NOT NULL
                            AND note.value >= ?
                            AND transaction_record.block IS NOT NULL
                            AND transaction_record.block <= ?
                            AND note.nf IS NOT NULL
                            AND note.commitment_tree_position IS NOT NULL
                            AND note.recipient_key_scope IS NOT NULL
                            AND note.lock_owner IS NULL
                            AND shard.max_priority = ?
                            AND NOT EXISTS (
                                SELECT 1
                                FROM v_ironwood_shard_unscanned_ranges
                                    AS unscanned
                                WHERE note.commitment_tree_position
                                    >= unscanned.start_position
                                AND note.commitment_tree_position
                                    < unscanned.end_position_exclusive
                            )
                            AND NOT EXISTS (
                                SELECT 1
                                FROM ironwood_received_note_spends AS spend
                                WHERE spend.ironwood_received_note_id = note.id
                            )
                        ORDER BY note.value DESC
                        LIMIT 1
                        """,
                        (
                            account_uuid,
                            minimum_value_zat,
                            anchor_height,
                            ZALLET_SCANNED_PRIORITY,
                        ),
                    ).fetchone()
                finally:
                    connection.close()
                if row is not None:
                    return {
                        "note_id": row[0],
                        "mined_height": row[1],
                        "value_zat": row[2],
                        "commitment_tree_position": row[3],
                        "shard_index": row[4],
                        "scan_priority": row[5],
                        "unscanned_ranges": 0,
                    }
            except sqlite3.Error:
                pass
            time.sleep(POLL_INTERVAL_SECONDS)
        raise RuntimeError(
            "producer Ironwood note did not become selectable at the "
            "confirmed anchor")

    def _source_unspent_outputs(self, wallet) -> list[dict]:
        return [
            {
                "txid": output["txid"],
                "outindex": output["outindex"],
                "address": output.get("address"),
                "account_uuid": output["account_uuid"],
                "valueZat": output["valueZat"],
            }
            for output in wallet.z_listunspent(
                MIN_CONFIRMATIONS,
                None,
                None,
                [self.transparent_source_address],
            )
            if output.get("pool") == Pool.TRANSPARENT
            and output.get("address") == self.transparent_source_address
        ]

    def _transparent_state(
            self,
            wallet,
            expected_txids: tuple[str, ...]) -> dict:
        transactions = {
            transaction["txid"]: transaction
            for transaction in wallet.z_listtransactions(
                self.producer_account_uuid)
        }
        history = [{
            "txid": txid,
            "mined_height": transactions[txid].get("mined_height"),
            "account_balance_delta": transactions[txid][
                "account_balance_delta"],
        } for txid in expected_txids if txid in transactions]
        balance = wallet.z_getbalanceforaccount(
            self.producer_account_uuid,
            MIN_CONFIRMATIONS,
        )
        return {
            "transparent_balance": {
                "valueZat": balance.get("pools", {})
                .get("transparent", {})
                .get("valueZat", 0),
                "minimum_confirmations": balance["minimum_confirmations"],
            },
            "source_utxos": sorted(
                self._source_unspent_outputs(wallet),
                key=lambda output: (output["txid"], output["outindex"]),
            ),
            "history": history,
        }

    def _wait_transparent_state(
            self,
            wallet,
            expected_source_utxos: int,
            expected_txids: tuple[str, ...],
            expected_state: dict | None = None) -> dict:
        deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
        previous = None
        while time.monotonic() < deadline:
            try:
                state = self._transparent_state(wallet, expected_txids)
                observed_txids = tuple(
                    transaction["txid"]
                    for transaction in state["history"]
                )
                shape_matches = (
                    len(state["source_utxos"]) == expected_source_utxos
                    and observed_txids == expected_txids
                )
                if shape_matches and expected_state is not None:
                    if state == expected_state:
                        return state
                elif shape_matches and state == previous:
                    return state
                previous = state
            except (OSError, JSONRPCException):
                pass
            time.sleep(1)
        raise RuntimeError(
            "transparent balance, UTXO, and history state did not converge")

    @staticmethod
    def _mined_history(state: dict) -> list[dict]:
        return [{
            "txid": transaction["txid"],
            "mined_height": transaction["mined_height"],
        } for transaction in state["history"]]

    def _wait_transparent_rescan_recovery(
            self,
            wallet,
            expected_txids: tuple[str, ...],
            expected_state: dict) -> dict:
        deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                state = self._transparent_state(wallet, expected_txids)
                if (
                    state["transparent_balance"]
                    == expected_state["transparent_balance"]
                    and state["source_utxos"]
                    == expected_state["source_utxos"]
                    and self._mined_history(state)
                    == self._mined_history(expected_state)
                ):
                    return state
            except (OSError, JSONRPCException):
                pass
            time.sleep(1)
        raise RuntimeError(
            "transparent rescan balance, spent status, and mined history "
            "did not converge")

    def _run_expiry_recovery(
            self,
            source,
            watcher,
            viewing_key: str,
            starting_tip: int) -> dict:
        assert self.runtime is not None and self.children is not None
        assert self.evidence is not None
        self._stop_wallet(source, "zallet-source-restarted")
        self._stop_wallet(watcher, "zallet-watcher")
        source_hashes = {
            height: self.nodes[0].getblockhash(height)
            for height in range(1, starting_tip + 1)
        }

        mined_history = self._mine_blocks(HISTORY_BLOCK_COUNT)
        history_tip = starting_tip + HISTORY_BLOCK_COUNT
        history_recovery_end_height = (
            history_tip - STEADY_STATE_REGION_BLOCKS - 1
        )
        if len(mined_history) != HISTORY_BLOCK_COUNT:
            raise RuntimeError("Zebra did not mine the requested history range")
        source_hashes.update({
            starting_tip + offset: block_hash
            for offset, block_hash in enumerate(mined_history, start=1)
        })
        self._wait_zinder_tip(history_tip)

        config = self._write_wallet_config(
            "expiry-recovery",
            recover_batch_size=10_000,
        )
        self._initialize_wallet(
            "expiry-recovery",
            config,
            generate_mnemonic=False,
        )
        prepared_wallet = self._start_wallet(
            "expiry-history-prepared",
            config,
        )
        self._wait_wallet_tip(prepared_wallet, history_tip)
        self._wait_for_backend_trace(
            "zallet-expiry-history-prepared",
            "following Zinder mempool after captured snapshot",
        )
        self._stop_wallet(
            prepared_wallet,
            "zallet-expiry-history-prepared",
        )
        history_blocks_before_import = self._scanned_blocks(config.parent)

        history_root = self.evidence / "expiry-recovery" / "history"
        history_barrier = history_root / "barrier"
        history_barrier.mkdir(parents=True)
        history_wallet = self._start_wallet(
            "expiry-history",
            config,
            self._range_barrier_environment(
                history_barrier,
                FULL_BLOCK_PAGE_SIZE + 1,
            ),
        )
        imported = history_wallet.z_importviewingkey(viewing_key, "yes", 1)
        if imported.get("address_type") != "sapling":
            raise RuntimeError("history-expiry import did not return a Sapling address")
        history_blocks_after_import = self._scanned_blocks(config.parent)
        self._require_range_absent(
            history_blocks_after_import,
            1,
            history_recovery_end_height,
            "history finalized range after import",
        )

        paused_marker = history_barrier / RANGE_REQUEST_PAUSED_MARKER
        self._wait_for_file(
            paused_marker,
            "zallet-expiry-history",
            "history range-request pause",
        )
        history_attempt_2 = self._read_range_marker(paused_marker)
        self._require_range_marker(
            history_attempt_2,
            FULL_BLOCK_PAGE_SIZE + 1,
            history_recovery_end_height,
            "history paused page",
        )
        history_attempt_2_path = (
            history_barrier
            / "range-request-attempt-{}.json".format(
                history_attempt_2["attempt_number"])
        )
        if self._read_range_marker(history_attempt_2_path) != history_attempt_2:
            raise RuntimeError(
                "history pause marker differs from its range-attempt marker")
        history_attempt_1 = self._find_range_marker(
            history_barrier,
            1,
            FULL_BLOCK_PAGE_SIZE,
            same_epoch=history_attempt_2["chain_epoch_id"],
            maximum_attempt=history_attempt_2["attempt_number"] - 1,
        )
        if history_attempt_1 is None:
            raise RuntimeError(
                "history page 1 was not observed in the paused page epoch")
        self._require_same_epoch(
            history_attempt_1,
            history_attempt_2,
            "history page pair",
        )
        history_blocks_paused = self._scanned_blocks(config.parent)
        self._require_range_absent(
            history_blocks_paused,
            1,
            history_recovery_end_height,
            "history finalized range while page 2 is paused",
        )

        history_rotated_tip, history_publications = self._rotate_pair(
            history_tip,
        )
        source_hashes[history_rotated_tip] = history_publications[
            "rotated_block_hash"
        ]
        self._release_range_request(
            history_barrier,
            history_tip,
            history_rotated_tip,
        )
        history_attempt_3 = self._wait_for_range_marker(
            history_barrier,
            1,
            FULL_BLOCK_PAGE_SIZE,
            "zallet-expiry-history",
            "history retry from page 1",
            different_epoch=history_attempt_2["chain_epoch_id"],
            minimum_attempt=history_attempt_2["attempt_number"] + 1,
        )
        self._wait_for_backend_trace(
            "zallet-expiry-history",
            "Chain view expired during history recovery; "
            "reacquiring it before retrying the entire range")
        self._wait_wallet_fully_synced(history_wallet, history_rotated_tip)
        history_blocks_final = self._scanned_blocks(config.parent)
        self._require_scanned_chain(
            history_blocks_final,
            source_hashes,
            history_rotated_tip,
            "history recovery",
        )
        self._stop_wallet(history_wallet, "zallet-expiry-history")

        steady_root = self.evidence / "expiry-recovery" / "steady"
        steady_barrier = steady_root / "barrier"
        steady_barrier.mkdir(parents=True)
        steady_wallet = self._start_wallet(
            "expiry-steady",
            config,
            self._range_barrier_environment(
                steady_barrier,
                history_rotated_tip + FULL_BLOCK_PAGE_SIZE + 1,
            ),
        )
        self._wait_wallet_fully_synced(steady_wallet, history_rotated_tip)
        self._wait_for_backend_trace(
            "zallet-expiry-steady",
            "following Zinder mempool after captured snapshot")
        steady_backend_pid = self._staged_backend_pid("zallet-expiry-steady")
        self._suspend_backend(steady_backend_pid)
        mined_steady = self._mine_blocks(STEADY_BLOCK_COUNT)
        steady_tip = history_rotated_tip + STEADY_BLOCK_COUNT
        if len(mined_steady) != STEADY_BLOCK_COUNT:
            raise RuntimeError("Zebra did not mine the requested steady-state range")
        source_hashes.update({
            history_rotated_tip + offset: block_hash
            for offset, block_hash in enumerate(mined_steady, start=1)
        })
        self._wait_zinder_tip(steady_tip)
        self._resume_backend(steady_backend_pid)

        steady_paused_marker = steady_barrier / RANGE_REQUEST_PAUSED_MARKER
        self._wait_for_file(
            steady_paused_marker,
            "zallet-expiry-steady",
            "steady-state range-request pause",
        )
        steady_attempt_2 = self._read_range_marker(steady_paused_marker)
        self._require_range_marker(
            steady_attempt_2,
            history_rotated_tip + FULL_BLOCK_PAGE_SIZE + 1,
            steady_tip,
            "steady-state paused page",
        )
        steady_attempt_2_path = (
            steady_barrier
            / "range-request-attempt-{}.json".format(
                steady_attempt_2["attempt_number"])
        )
        if self._read_range_marker(steady_attempt_2_path) != steady_attempt_2:
            raise RuntimeError(
                "steady-state pause marker differs from its range-attempt marker")
        steady_attempt_1 = self._find_range_marker(
            steady_barrier,
            history_rotated_tip + 1,
            history_rotated_tip + FULL_BLOCK_PAGE_SIZE,
            same_epoch=steady_attempt_2["chain_epoch_id"],
            maximum_attempt=steady_attempt_2["attempt_number"] - 1,
        )
        if steady_attempt_1 is None:
            raise RuntimeError(
                "steady-state page 1 was not observed in the paused page epoch")
        self._require_same_epoch(
            steady_attempt_1,
            steady_attempt_2,
            "steady-state page pair",
        )
        steady_blocks_paused = self._scanned_blocks(config.parent)
        committed_prefix_tip = history_rotated_tip + FULL_BLOCK_PAGE_SIZE
        self._require_scanned_chain(
            steady_blocks_paused,
            source_hashes,
            committed_prefix_tip,
            "steady-state committed prefix",
        )
        if any(height == committed_prefix_tip + 1 for height, _ in steady_blocks_paused):
            raise RuntimeError(
                "steady-state sync committed the page-2 successor before expiry")

        steady_rotated_tip, steady_publications = self._rotate_pair(steady_tip)
        source_hashes[steady_rotated_tip] = steady_publications[
            "rotated_block_hash"
        ]
        self._release_range_request(
            steady_barrier,
            steady_tip,
            steady_rotated_tip,
        )
        steady_attempt_3 = self._wait_for_range_marker(
            steady_barrier,
            committed_prefix_tip + 1,
            steady_rotated_tip,
            "zallet-expiry-steady",
            "steady-state successor retry",
            different_epoch=steady_attempt_2["chain_epoch_id"],
            minimum_attempt=steady_attempt_2["attempt_number"] + 1,
        )
        self._wait_for_backend_trace(
            "zallet-expiry-steady",
            "Chain view expired, reacquiring the current view")
        self._wait_wallet_fully_synced(steady_wallet, steady_rotated_tip)
        steady_blocks_final = self._scanned_blocks(config.parent)
        self._require_scanned_chain(
            steady_blocks_final,
            source_hashes,
            steady_rotated_tip,
            "steady-state recovery",
        )
        self._stop_wallet(steady_wallet, "zallet-expiry-steady")

        result = {
            "history": {
                "initial_tip": history_tip,
                "finalized_recovery_end_height": (
                    history_recovery_end_height
                ),
                "rotated_tip": history_rotated_tip,
                "blocks_before_import": self._scanned_block_summary(
                    history_blocks_before_import),
                "blocks_after_import": self._scanned_block_summary(
                    history_blocks_after_import),
                "blocks_while_page_2_paused": self._scanned_block_summary(
                    history_blocks_paused),
                "blocks_after_recovery": self._scanned_block_summary(
                    history_blocks_final),
                "partial_block_writes_before_expiry": False,
                "final_chain_contiguous_and_zebra_hash_matching": True,
                "range_attempts": [
                    history_attempt_1,
                    history_attempt_2,
                    history_attempt_3,
                ],
                "typed_expiry_trace": (
                    "Chain view expired during history recovery; "
                    "reacquiring it before retrying the entire range"),
                "pair_publications": history_publications,
            },
            "steady_state": {
                "initial_tip": history_rotated_tip,
                "surge_tip": steady_tip,
                "committed_prefix_tip": committed_prefix_tip,
                "rotated_tip": steady_rotated_tip,
                "staged_backend_pid": steady_backend_pid,
                "blocks_while_page_2_paused": self._scanned_block_summary(
                    steady_blocks_paused),
                "blocks_after_recovery": self._scanned_block_summary(
                    steady_blocks_final),
                "committed_prefix_contiguous_and_zebra_hash_matching": True,
                "page_2_successor_committed_before_expiry": False,
                "final_chain_contiguous_and_zebra_hash_matching": True,
                "range_attempts": [
                    steady_attempt_1,
                    steady_attempt_2,
                    steady_attempt_3,
                ],
                "typed_expiry_trace": (
                    "Chain view expired, reacquiring the current view"),
                "pair_publications": steady_publications,
            },
        }
        self._write_json("expiry-recovery.json", result)
        return result

    def _range_barrier_environment(
            self,
            barrier: Path,
            pause_start_height: int) -> dict[str, str]:
        return {
            RANGE_BARRIER_DIRECTORY_ENVIRONMENT: str(barrier),
            RANGE_REQUEST_PAUSE_START_HEIGHT_ENVIRONMENT: str(
                pause_start_height),
        }

    def _wait_zinder_tip(self, height: int):
        assert self.children is not None
        for service in ("ingest", "projector", "query"):
            self._wait_ready(
                service,
                self.children.children["zinder-" + service],
                height,
            )

    def _mine_blocks(self, count: int) -> list[str]:
        mined = []
        while len(mined) < count:
            batch_size = min(BLOCK_GENERATION_BATCH_SIZE, count - len(mined))
            batch = self.nodes[0].generate(batch_size)
            if len(batch) != batch_size:
                raise RuntimeError(
                    "Zebra mined {} blocks for a requested batch of {}".format(
                        len(batch), batch_size))
            mined.extend(batch)
        return mined

    def _rotate_pair(self, previous_tip: int) -> tuple[int, dict]:
        assert self.runtime is not None and self.children is not None
        publication_before = self._pair_publication_count()
        mined = self.nodes[0].generate(1)
        rotated_tip = previous_tip + 1
        if len(mined) != 1 or self.nodes[0].getblockcount() != rotated_tip:
            raise RuntimeError("Zebra did not perform the single-block epoch rotation")
        self._wait_zinder_tip(rotated_tip)
        publication_after = self._wait_pair_publication(publication_before)
        time.sleep(INGEST_CONTROL_ADMISSION_SETTLE_SECONDS)
        self._wait_ready(
            "query",
            self.children.children["zinder-query"],
            rotated_tip,
        )
        return rotated_tip, {
            "metric": PAIR_PUBLICATION_METRIC,
            "before": publication_before,
            "after": publication_after,
            "rotated_block_hash": mined[0],
        }

    def _pair_publication_count(self) -> float:
        assert self.runtime is not None
        status, metrics = http_text(self.runtime.ops["query"] + "/metrics")
        if status != 200:
            raise RuntimeError("zinder-query metrics endpoint was not available")
        series = [
            line for line in metrics.splitlines()
            if re.match(
                r"^{}(?:\{{|\s|$)".format(re.escape(PAIR_PUBLICATION_METRIC)),
                line)
        ]
        if len(series) != 1:
            raise RuntimeError(
                "expected one {} measurement, got {}".format(
                    PAIR_PUBLICATION_METRIC, series))
        measurement = re.fullmatch(
            r"{}(?:\{{[^}}]*\}})?\s+([0-9eE+.-]+)".format(
                re.escape(PAIR_PUBLICATION_METRIC)),
            series[0])
        if measurement is None:
            raise RuntimeError(
                "malformed {} measurement".format(PAIR_PUBLICATION_METRIC))
        value = float(measurement.group(1))
        if not math.isfinite(value):
            raise RuntimeError(
                "{} measurement is not finite".format(PAIR_PUBLICATION_METRIC))
        return value

    def _wait_pair_publication(self, previous: float) -> float:
        assert self.children is not None
        expected = previous + 1
        deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
        latest = previous
        while time.monotonic() < deadline:
            child = self.children.children["zinder-query"]
            if child.poll() is not None:
                raise RuntimeError("zinder-query exited before pair publication")
            try:
                latest = self._pair_publication_count()
                if latest == expected:
                    return latest
                if latest != previous:
                    raise RuntimeError(
                        "{} changed from {} to {}, expected {}".format(
                            PAIR_PUBLICATION_METRIC, previous, latest, expected))
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(POLL_INTERVAL_SECONDS)
        raise RuntimeError(
            "{} did not advance from {} to {}; last value was {}".format(
                PAIR_PUBLICATION_METRIC, previous, expected, latest))

    def _release_range_request(
            self,
            barrier: Path,
            expired_tip: int,
            rotated_tip: int):
        (barrier / RANGE_REQUEST_CONTINUE_MARKER).write_text(
            json.dumps({
                "schema_version": 1,
                "expired_tip": expired_tip,
                "rotated_tip": rotated_tip,
            }, sort_keys=True) + "\n",
            encoding="utf8")

    def _read_range_marker(self, path: Path) -> dict:
        marker = self._read_json(path)
        if set(marker) != RANGE_MARKER_FIELDS:
            raise RuntimeError(
                "{} has unexpected range-marker fields".format(path))
        for field in (
                "schema_version",
                "attempt_number",
                "requested_start_height_inclusive",
                "requested_end_height_inclusive",
                "chain_epoch_id"):
            value = marker[field]
            if not isinstance(value, int) or isinstance(value, bool):
                raise RuntimeError(
                    "{} field {} is not an integer".format(path, field))
        if marker["schema_version"] != 1:
            raise RuntimeError("{} has unsupported schema version".format(path))
        if marker["attempt_number"] < 1:
            raise RuntimeError("{} has an invalid attempt number".format(path))
        if (marker["requested_start_height_inclusive"] < 0
                or marker["requested_end_height_inclusive"]
                < marker["requested_start_height_inclusive"]):
            raise RuntimeError(
                "{} has an invalid requested range".format(path))
        attempt_filename = re.fullmatch(
            r"range-request-attempt-([0-9]+)\.json",
            path.name,
        )
        if (attempt_filename is not None
                and int(attempt_filename.group(1))
                != marker["attempt_number"]):
            raise RuntimeError(
                "{} attempt number differs from its filename".format(path))
        return marker

    def _require_range_marker(
            self,
            marker: dict,
            start_height: int,
            end_height: int,
            description: str):
        observed = (
            marker["requested_start_height_inclusive"],
            marker["requested_end_height_inclusive"],
        )
        expected = (start_height, end_height)
        if observed != expected:
            raise RuntimeError(
                "{} range is {}, expected {}".format(
                    description, observed, expected))

    def _find_range_marker(
            self,
            barrier: Path,
            start_height: int,
            end_height: int,
            *,
            same_epoch: int | None = None,
            different_epoch: int | None = None,
            minimum_attempt: int = 1,
            maximum_attempt: int | None = None) -> dict | None:
        matches = []
        for path in sorted(barrier.glob("range-request-attempt-*.json")):
            marker = self._read_range_marker(path)
            attempt = marker["attempt_number"]
            if attempt < minimum_attempt:
                continue
            if maximum_attempt is not None and attempt > maximum_attempt:
                continue
            if marker["requested_start_height_inclusive"] != start_height:
                continue
            if marker["requested_end_height_inclusive"] != end_height:
                continue
            epoch = marker["chain_epoch_id"]
            if same_epoch is not None and epoch != same_epoch:
                continue
            if different_epoch is not None and epoch == different_epoch:
                continue
            matches.append(marker)
        if not matches:
            return None
        return min(matches, key=lambda marker: marker["attempt_number"])

    def _wait_for_range_marker(
            self,
            barrier: Path,
            start_height: int,
            end_height: int,
            process_name: str,
            description: str,
            *,
            same_epoch: int | None = None,
            different_epoch: int | None = None,
            minimum_attempt: int = 1,
            maximum_attempt: int | None = None) -> dict:
        assert self.children is not None
        deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
        child = self.children.children[process_name]
        while time.monotonic() < deadline:
            marker = self._find_range_marker(
                barrier,
                start_height,
                end_height,
                same_epoch=same_epoch,
                different_epoch=different_epoch,
                minimum_attempt=minimum_attempt,
                maximum_attempt=maximum_attempt,
            )
            if marker is not None:
                return marker
            if child.poll() is not None:
                raise RuntimeError(
                    "{} exited before {}".format(process_name, description))
            time.sleep(POLL_INTERVAL_SECONDS)
        raise RuntimeError(
            "timed out waiting for {} in {}".format(description, barrier))

    def _read_json(self, path: Path) -> dict:
        with path.open("r", encoding="utf8") as source:
            document = json.load(source)
        if not isinstance(document, dict):
            raise RuntimeError("{} did not contain a JSON object".format(path))
        return document

    def _wait_for_file(self, path: Path, process_name: str, description: str):
        assert self.children is not None
        deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
        child = self.children.children[process_name]
        while time.monotonic() < deadline:
            if path.is_file():
                return
            if child.poll() is not None:
                raise RuntimeError(
                    "{} exited before {}".format(process_name, description))
            time.sleep(POLL_INTERVAL_SECONDS)
        raise RuntimeError("timed out waiting for {}: {}".format(description, path))

    def _scanned_blocks(self, wallet_directory: Path) -> list[tuple[int, str]]:
        wallet_database = wallet_directory / "wallet.db"
        connection = sqlite3.connect(
            "file:{}?mode=ro".format(wallet_database),
            uri=True,
        )
        try:
            return [
                (row[0], bytes(row[1])[::-1].hex())
                for row in connection.execute(
                    "SELECT height, hash FROM blocks ORDER BY height")
            ]
        finally:
            connection.close()

    def _scanned_block_summary(self, blocks: list[tuple[int, str]]) -> dict:
        return {
            "count": len(blocks),
            "minimum_height": None if not blocks else blocks[0][0],
            "maximum_height": None if not blocks else blocks[-1][0],
            "maximum_height_hash": None if not blocks else blocks[-1][1],
        }

    def _require_range_absent(
            self,
            blocks: list[tuple[int, str]],
            start_height: int,
            end_height: int,
            description: str):
        observed = [
            height
            for height, _ in blocks
            if start_height <= height <= end_height
        ]
        if observed:
            raise RuntimeError(
                "{} unexpectedly contains {} rows from {} through {}".format(
                    description,
                    len(observed),
                    observed[0],
                    observed[-1],
                ))

    def _require_scanned_chain(
            self,
            blocks: list[tuple[int, str]],
            source_hashes: dict[int, str],
            expected_tip: int,
            description: str):
        expected_heights = list(range(1, expected_tip + 1))
        actual_heights = [height for height, _ in blocks]
        if actual_heights != expected_heights:
            raise RuntimeError(
                "{} block heights are not contiguous from 1 through {}; "
                "observed {} through {} across {} rows".format(
                    description,
                    expected_tip,
                    None if not blocks else blocks[0][0],
                    None if not blocks else blocks[-1][0],
                    len(blocks)))
        for height, block_hash in blocks:
            if block_hash != source_hashes[height]:
                raise RuntimeError(
                    "{} block hash at height {} differs from Zebra".format(
                        description, height))

    def _require_same_epoch(
            self,
            first: dict,
            second: dict,
            description: str):
        if first["chain_epoch_id"] != second["chain_epoch_id"]:
            raise RuntimeError(
                "{} did not retain one chain epoch across both pages".format(
                    description))

    def _stop_wallet(self, wallet, process_name: str):
        assert self.children is not None
        wallet.stop()
        exit_code = self.children.stop(process_name)
        if exit_code != 0:
            raise RuntimeError(
                "{} did not stop cleanly: {}".format(process_name, exit_code))

    def _staged_backend_pid(
            self,
            process_name: str,
            expected: Path | None = None) -> int:
        assert self.children is not None and self.runtime is not None
        child = self.children.children[process_name]
        if expected is None:
            expected = (
                self.runtime.root / "staged-zallet" / "zallet-zinder"
            )
        expected = expected.resolve()
        deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError(
                    "{} exited before staged backend verification".format(
                        process_name))
            try:
                actual = Path(os.readlink("/proc/{}/exe".format(child.pid))).resolve()
                if actual == expected:
                    return child.pid
            except FileNotFoundError:
                pass
            time.sleep(POLL_INTERVAL_SECONDS)
        raise RuntimeError(
            "{} did not exec the staged backend {}".format(process_name, expected))

    def _suspend_backend(self, pid: int):
        os.kill(pid, signal.SIGSTOP)
        self.suspended_backend_pids.add(pid)

    def _resume_backend(self, pid: int):
        try:
            os.kill(pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
        self.suspended_backend_pids.discard(pid)

    def _resume_suspended_backends(self):
        for pid in tuple(self.suspended_backend_pids):
            self._resume_backend(pid)

    def _render_runtime(self, root: Path) -> RuntimePaths:
        assert self.evidence is not None
        (root / "checkpoints").mkdir(parents=True, exist_ok=False)
        ports = [allocate_port(self.allocated_ports) for _ in range(5)]
        control_port, ingest_ops_port, projector_ops_port, query_port, query_ops_port = ports
        control_endpoint = "http://127.0.0.1:{}".format(control_port)
        node_endpoint = rpc_url(0)
        node_health = self.zebra_health_endpoint
        paths = {
            "ingest": root / "ingest.toml",
            "projector": root / "projector.toml",
            "query": root / "query.toml",
        }
        arguments = {
            "ingest": (["--network", NETWORK_NAME, "--node-source", "zebra-json-rpc", "--json-rpc-addr", node_endpoint,
                        "--node-auth-method", "none", "--storage-path", str(root / "canonical"), "--reorg-window-blocks", "100",
                        "--wallet-serving", "--ingest-control-listen-addr", "127.0.0.1:{}".format(control_port),
                        "--ops-listen-addr", "127.0.0.1:{}".format(ingest_ops_port)],
                       {"ZINDER_NODE__HEALTH__ADDR": node_health, "ZINDER_STORAGE__RAW_BLOB_POLICY": "all",
                        "ZINDER_INGEST_CONTROL__CHECKPOINT_STAGING_ROOT": str(root / "checkpoints")}),
            "projector": (["--network", NETWORK_NAME, "--canonical-path", str(root / "canonical"),
                          "--canonical-secondary-path", str(root / "projector-canonical-secondary"), "--wallet-path", str(root / "wallet"),
                          "--reorg-window-blocks", "100", "--build-owner-hex", "30303030303030303030303030303030",
                          "--lease-duration-seconds", "14400", "--node-json-rpc-addr", node_endpoint,
                          "--ingest-control-addr", control_endpoint, "--ops-listen-addr", "127.0.0.1:{}".format(projector_ops_port)],
                         {"ZINDER_NODE__HEALTH__ADDR": node_health, "ZINDER_STORAGE__RAW_BLOB_POLICY": "all",
                          "ZINDER_PROJECTOR_CONTROL__CHECKPOINT_STAGING_ROOT": str(root / "checkpoints")}),
            "query": (["--network", NETWORK_NAME, "--canonical-primary-path", str(root / "canonical"),
                       "--canonical-secondary-root", str(root / "query-canonical-secondary"), "--raw-blob-policy", "all",
                       "--wallet-primary-path", str(root / "wallet"), "--wallet-secondary-root", str(root / "query-wallet-secondary"),
                       "--ingest-control-addr", control_endpoint, "--listen-addr", "127.0.0.1:{}".format(query_port),
                       "--reorg-window-blocks", "100", "--ops-listen-addr", "127.0.0.1:{}".format(query_ops_port),
                       "--node-json-rpc-addr", node_endpoint], {"ZINDER_NODE__HEALTH__ADDR": node_health}),
        }
        for service, (flags, extra_environment) in arguments.items():
            environment = clean_zinder_environment()
            environment.update(extra_environment)
            completed = subprocess.run([self.artifacts["zinder_" + service], "--print-config", *flags], env=environment,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
            if completed.returncode != 0:
                raise RuntimeError("{} config render failed: {}".format(service, completed.stderr))
            toml.loads(completed.stdout)
            paths[service].parent.mkdir(parents=True, exist_ok=True)
            paths[service].write_text(completed.stdout, encoding="utf8")
            shutil.copyfile(paths[service], self.evidence / ("zinder-" + service + ".toml"))
        return RuntimePaths(root, root / "canonical", root / "wallet", paths,
                            {"ingest": "http://127.0.0.1:{}".format(ingest_ops_port),
                             "projector": "http://127.0.0.1:{}".format(projector_ops_port),
                             "query": "http://127.0.0.1:{}".format(query_ops_port)},
                            "http://127.0.0.1:{}".format(query_port), control_endpoint)

    def _write_wallet_config(
            self,
            name: str,
            recover_batch_size: int | None = None,
            clone_from: Path | None = None) -> Path:
        assert self.runtime is not None and self.evidence is not None
        wallet_root = self.runtime.root / "wallets" / name
        if clone_from is not None:
            shutil.copytree(clone_from, wallet_root)
        wallet_port = allocate_port(self.allocated_ports)
        previous_backend = os.environ.get("ZALLET_BACKEND")
        os.environ["ZALLET_BACKEND"] = "zinder"
        try:
            config_path = update_zallet_conf(
                str(wallet_root), rpc_port(0), wallet_port,
                ZalletArgs(activation_heights=self.activation_heights),
                zinder_endpoint=self.runtime.query_endpoint)
        finally:
            if previous_backend is None:
                del os.environ["ZALLET_BACKEND"]
            else:
                os.environ["ZALLET_BACKEND"] = previous_backend
        config = toml.load(config_path)
        config.setdefault("keystore", {})["encryption_identity"] = str(wallet_root / "encryption-identity.txt")
        config["rpc"]["bind"] = ["127.0.0.1:{}".format(wallet_port)]
        if recover_batch_size is not None:
            config.setdefault("sync", {})["recover_batch_size"] = recover_batch_size
        path = Path(config_path)
        path.write_text(toml.dumps(config), encoding="utf8")
        redacted = dict(config)
        redacted["indexer"] = {"validator_address": "redacted"}
        (self.evidence / ("zallet-" + name + ".toml")).write_text(toml.dumps(redacted), encoding="utf8")
        return path

    def _initialize_wallet(self, name: str, config: Path, generate_mnemonic: bool):
        # The test-framework default config supplies a disposable encryption
        # identity with the copied datadir, so generating another would rightly
        # fail rather than overwrite it.
        commands = [("init-wallet-encryption",)]
        if generate_mnemonic:
            commands.append(("generate-mnemonic",))
        seedfp = None
        for arguments in commands:
            completed = subprocess.run([self._staged_launcher(), "--datadir={}".format(config.parent), "--config={}".format(config), *arguments],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
            if completed.returncode != 0:
                raise RuntimeError("{} wallet initialization command {} failed: {}".format(name, " ".join(arguments), completed.stderr))
            if arguments == ("generate-mnemonic",):
                seedfp = completed.stdout.strip().rsplit(": ", 1)[-1]
                if not seedfp.startswith("zip32seedfp1"):
                    raise RuntimeError("{} wallet did not report a seed fingerprint".format(name))
        if not (config.parent / "wallet.db").is_file():
            raise RuntimeError("{} wallet initialization did not create wallet.db".format(name))
        if generate_mnemonic and seedfp is None:
            raise RuntimeError("{} wallet did not generate a seed fingerprint".format(name))
        return seedfp

    def _create_live_accounts(self, wallet, seedfp: str):
        names = ("Zinder P2a creator 0", "Zinder P2a creator 1")
        created = [wallet.z_getnewaccount(name, seedfp) for name in names]
        account_uuids = [result.get("account_uuid") for result in created]
        if len(set(account_uuids)) != len(names):
            raise RuntimeError("z_getnewaccount did not return distinct account UUIDs")
        try:
            for account_uuid in account_uuids:
                uuid.UUID(account_uuid)
        except (AttributeError, TypeError, ValueError) as error:
            raise RuntimeError("z_getnewaccount returned a non-UUID account identifier") from error

        listed_by_uuid = {
            account["account_uuid"]: account for account in wallet.z_listaccounts(False)
        }
        if set(listed_by_uuid) != set(account_uuids):
            raise RuntimeError("live account creation did not produce exactly two accounts")

        records = []
        for index, (name, account_uuid) in enumerate(zip(names, account_uuids)):
            listed = listed_by_uuid[account_uuid]
            if (listed.get("name"), listed.get("seedfp"), listed.get("zip32_account_index")) != (
                    name, seedfp, index):
                raise RuntimeError("live account metadata does not match its creation request")
            address = wallet.z_getaddressforaccount(account_uuid, ["sapling"], None)
            if address.get("account_uuid") != account_uuid or address.get("receiver_types") != ["sapling"]:
                raise RuntimeError("live account did not derive the requested Sapling-only address")
            receivers = wallet.z_listunifiedreceivers(address["address"])
            if set(receivers) != {"sapling"}:
                raise RuntimeError("derived live-account address was not Sapling-only")
            viewing_key = wallet.z_exportviewingkey(receivers["sapling"], False)
            records.append({
                "account_uuid": account_uuid,
                "name": name,
                "seedfp": seedfp,
                "zip32_account_index": index,
                "sapling_address": receivers["sapling"],
                "viewing_key": viewing_key,
            })

        if len({record["sapling_address"] for record in records}) != len(records):
            raise RuntimeError("live accounts produced duplicate Sapling addresses")
        if len({record["viewing_key"] for record in records}) != len(records):
            raise RuntimeError("live accounts produced duplicate Sapling viewing keys")
        return records

    def _assert_live_accounts(self, wallet, expected):
        listed = wallet.z_listaccounts(False)
        actual = sorted(
            [{
                "account_uuid": account["account_uuid"],
                "name": account.get("name"),
                "seedfp": account.get("seedfp"),
                "zip32_account_index": account.get("zip32_account_index"),
            } for account in listed],
            key=lambda account: account["zip32_account_index"])
        expected_accounts = [{
            "account_uuid": account["account_uuid"],
            "name": account["name"],
            "seedfp": account["seedfp"],
            "zip32_account_index": account["zip32_account_index"],
        } for account in expected]
        if actual != expected_accounts:
            raise RuntimeError("live accounts changed across sync or restart")
        return actual

    def _start_wallet(
            self,
            name: str,
            config: Path,
            environment_overrides: dict[str, str] | None = None,
            launcher: str | None = None):
        assert self.children is not None
        if name == "source-restarted":
            process_name = "zallet-source-restarted"
        else:
            process_name = "zallet-" + name
        if launcher is None:
            launcher = self._staged_launcher()
        environment = dict(os.environ)
        if environment_overrides is not None:
            environment.update(environment_overrides)
        child = self.children.start(
            process_name,
            [launcher, "--datadir={}".format(config.parent),
             "--config={}".format(config), "start"],
            environment)
        port = toml.load(config)["rpc"]["bind"][0].rsplit(":", 1)[1]
        endpoint = "http://zebra:zebra@127.0.0.1:{}".format(port)
        deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError("{} exited before JSON-RPC readiness".format(process_name))
            try:
                wallet = get_rpc_auth_proxy(
                    endpoint,
                    100 if name.startswith("source") else 101,
                    timeout=HTTP_TIMEOUT_SECONDS,
                )
                wallet.getwalletinfo()
                return wallet
            except (OSError, JSONRPCException):
                time.sleep(POLL_INTERVAL_SECONDS)
        raise RuntimeError("{} did not become JSON-RPC ready".format(process_name))

    def _staged_launcher(self) -> str:
        assert self.runtime is not None and self.evidence is not None
        stage = self.runtime.root / "staged-zallet"
        stage.mkdir(exist_ok=True)
        launcher = stage / "zallet"
        backend = stage / "zallet-zinder"
        if not launcher.exists():
            shutil.copy2(self.artifacts["zallet"], launcher)
            shutil.copy2(self.artifacts["zallet_zinder"], backend)
            launcher.chmod(0o755)
            backend.chmod(0o755)
            self._write_json("staged-zallet.json", {
                "launcher": sha256_file(launcher),
                "sibling": sha256_file(backend),
            })
        if not backend.is_file() or not os.access(backend, os.X_OK):
            raise RuntimeError("Zallet launcher sibling is not staged and executable")
        return str(launcher)

    def _staged_producer_launcher(self) -> str:
        assert self.evidence is not None
        stage = Path(self.options.tmpdir) / "staged-producer-zallet"
        stage.mkdir(exist_ok=True)
        launcher = stage / "zallet"
        backend = stage / "zallet-zebra"
        if not launcher.exists():
            shutil.copy2(self.artifacts["zallet"], launcher)
            shutil.copy2(self.artifacts["zallet_zebra"], backend)
            launcher.chmod(0o755)
            backend.chmod(0o755)
            self._write_json("staged-producer-zallet.json", {
                "launcher": sha256_file(launcher),
                "sibling": sha256_file(backend),
            })
        if not backend.is_file() or not os.access(backend, os.X_OK):
            raise RuntimeError(
                "Zallet Zebra producer sibling is not staged and executable")
        return str(launcher)

    def _staged_producer_backend(self) -> Path:
        return (
            Path(self._staged_producer_launcher()).parent / "zallet-zebra"
        )

    def _staged_target_files(self) -> list[str]:
        assert self.runtime is not None
        files = sorted(
            path.name
            for path in (self.runtime.root / "staged-zallet").iterdir()
        )
        if files != ["zallet", "zallet-zinder"]:
            raise RuntimeError(
                "Zinder target stage has unexpected siblings: {}".format(
                    files))
        return files

    def _staged_producer_files(self) -> list[str]:
        files = sorted(
            path.name
            for path in Path(
                self._staged_producer_launcher()).parent.iterdir()
        )
        if files != ["zallet", "zallet-zebra"]:
            raise RuntimeError(
                "Zebra producer stage has unexpected siblings: {}".format(
                    files))
        return files

    def _health(self, service: str) -> dict:
        assert self.runtime is not None
        status, body = http_json(self.runtime.ops[service] + "/healthz")
        if status != 200 or body.get("status") != "alive":
            raise RuntimeError("zinder-{} health failure: {} {}".format(service, status, body))
        if body.get("git_commit") != ZINDER_REVISION:
            raise RuntimeError("zinder-{} reports unexpected git commit {}".format(service, body.get("git_commit")))
        return body

    def _wait_ready(self, service: str, child: subprocess.Popen, minimum_height: int):
        assert self.runtime is not None
        deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError("zinder-{} exited before readiness".format(service))
            try:
                self._health(service)
                status, body = http_json(self.runtime.ops[service] + "/readyz")
                if status == 200 and body.get("status") == "ready" and body.get("current_height", -1) >= minimum_height:
                    return
            except (OSError, ValueError, urllib.error.URLError):
                pass
            time.sleep(POLL_INTERVAL_SECONDS)
        raise RuntimeError("zinder-{} did not reach height {}".format(service, minimum_height))

    def _wait_wallet_tip(self, wallet, height: int):
        deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            status = wallet.getwalletstatus()
            if status.get("wallet_tip", {}).get("height") == height:
                return
            time.sleep(POLL_INTERVAL_SECONDS)
        raise RuntimeError("wallet did not reach height {}".format(height))

    def _wait_wallet_fully_synced(self, wallet, height: int):
        deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            status = wallet.getwalletstatus()
            if (status.get("wallet_tip", {}).get("height") == height
                    and status.get("fully_synced_height") == height
                    and "sync_work_remaining" not in status):
                return status
            time.sleep(POLL_INTERVAL_SECONDS)
        raise RuntimeError("wallet did not fully sync to height {}".format(height))

    def _wait_for_backend_trace(self, process_name: str, expected: str):
        assert self.children is not None
        deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
        stdout_path, stderr_path = self.children.paths[process_name]
        while time.monotonic() < deadline:
            for path in (stdout_path, stderr_path):
                if path.exists() and expected in path.read_text(errors="replace"):
                    return
            child = self.children.children[process_name]
            if child.poll() is not None:
                raise RuntimeError("{} exited before quiet-mempool trace".format(process_name))
            time.sleep(POLL_INTERVAL_SECONDS)
        raise RuntimeError("{} did not report {}".format(process_name, expected))

    def _record_artifacts(self):
        assert self.evidence is not None
        self._write_json("artifacts.json", {"artifacts": {name: {"path": str(path), "sha256": self.artifact_hashes[name]}
                                                              for name, path in self.artifacts.items()},
                                              "zinder_revision": ZINDER_REVISION, "zallet_revision": ZALLET_REVISION,
                                              "zebra_revision": os.environ["ZIT_ZEBRA_REVISION"]})

    def _write_json(self, name: str, value: dict):
        assert self.evidence is not None
        (self.evidence / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf8")

    def _cleanup(self):
        errors = []
        self._resume_suspended_backends()
        if self.children is not None:
            try:
                exits = self.children.stop_all()
                self._write_json("process-exits.json", exits)
                unexpected_exits = {
                    name: code for name, code in exits.items() if code != 0
                }
                if unexpected_exits:
                    errors.append(
                        "owned processes exited unsuccessfully: {}".format(
                            unexpected_exits))
            except Exception as error:
                errors.append(str(error))
        if self.zebra_process is not None:
            if self.zebra_process.poll() is None:
                try:
                    self.nodes[0].stop()
                except Exception:
                    self.zebra_process.terminate()
                try:
                    self.zebra_process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    self.zebra_process.kill()
                    self.zebra_process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
            if self.zebra_process.returncode != 0:
                errors.append(
                    "Zebra exited unsuccessfully: {}".format(
                        self.zebra_process.returncode))
            bitcoind_processes.pop(0, None)
            if self.zebra_files is not None and self.zebra_logs is not None:
                for descriptor, source in zip(self.zebra_files, self.zebra_logs):
                    descriptor.close()
                    assert self.evidence is not None
                    shutil.copyfile(source, self.evidence / source.name)
            self.nodes.clear()
        return RuntimeError("; ".join(errors)) if errors else None

    def _write_manifest(self, failure, cleanup_error):
        if self.evidence is not None:
            selected_slice = certification_slice()
            scope = {
                "p2b": "P2b transparent recovery",
                "p2c": "P2b fixture plus P2c submit lifecycle",
                "complete": (
                    "P2a expiry recovery plus P2b transparent recovery "
                    "plus P2c submit lifecycle"
                ),
            }[selected_slice]
            self._write_json("manifest.json", {"result": "passed" if failure is None and cleanup_error is None else "failed",
                                                 "failure": None if failure is None else str(failure),
                                                 "cleanup_failure": None if cleanup_error is None else str(cleanup_error),
                                                 "scope": scope})


if __name__ == '__main__':
    WalletZinderRecoverySmoke().main()
