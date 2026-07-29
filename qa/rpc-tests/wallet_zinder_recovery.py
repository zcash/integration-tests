#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

"""Exercise current Zallet recovery against a fresh native Zinder runtime.

The runtime smoke includes the two P2a chain-view expiry proofs.  Transaction
submission, transparent-history correctness, and an Ironwood transaction
producer remain outside this scenario.
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
from pathlib import Path

import toml

from test_framework.config import ZebraArgs, ZalletArgs
from test_framework.proxy import JSONRPCException
from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import (
    bitcoind_processes,
    get_rpc_auth_proxy,
    get_rpc_proxy,
    indexer_rpc_port,
    node_dir,
    p2p_port,
    rpc_port,
    rpc_url,
    update_zallet_conf,
    update_zebrad_conf,
)


NETWORK_NAME = "zcash-regtest"
INITIAL_HEIGHT = 3
READINESS_TIMEOUT_SECONDS = 240
PROCESS_STOP_TIMEOUT_SECONDS = 15
# Keep individual transport failures bounded while allowing normal wallet
# database and key operations to complete.
HTTP_TIMEOUT_SECONDS = 30
POLL_INTERVAL_SECONDS = 0.2
FULL_BLOCK_PAGE_SIZE = 1_000
STEADY_STATE_REGION_BLOCKS = 100
HISTORY_BLOCK_COUNT = (
    FULL_BLOCK_PAGE_SIZE + STEADY_STATE_REGION_BLOCKS + 1
)
STEADY_BLOCK_COUNT = FULL_BLOCK_PAGE_SIZE + 1
BLOCK_GENERATION_BATCH_SIZE = 100
PAIR_PUBLICATION_METRIC = "zinder_wallet_serving_pair_publisher_publications_total"
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
ZINDER_REVISION = "515b98b490575a87d8cd629d01551114f42a5735"
ZALLET_REVISION = "05fcb9ec374cb4fdc2c230b8d572570fad176a80"
ZEBRA_REVISION = "7121c82ba6795a151523d5b308a19743f4a1ade7"
EVIDENCE_DIRECTORY_ENVIRONMENT = "ZIT_ZINDER_EVIDENCE_DIR"

# This is the current Zallet P2a requirement constant.  The endpoint can
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

    def setup_chain(self):
        super().setup_chain()
        self.artifacts, self.artifact_hashes = required_artifacts()
        self.evidence = require_evidence_directory()
        self._record_artifacts()

    def setup_nodes(self):
        self.allocated_ports.update((rpc_port(0), p2p_port(0), indexer_rpc_port(0)))
        health_port = allocate_port(self.allocated_ports)
        health_address = "127.0.0.1:{}".format(health_port)
        data_directory = Path(node_dir(self.options.tmpdir, 0))
        config_path = Path(update_zebrad_conf(
            str(data_directory), rpc_port(0), p2p_port(0), indexer_rpc_port(0),
            ZebraArgs(activation_heights=self.activation_heights)))
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

        source_config = self._write_wallet_config("source")
        watcher_config = self._write_wallet_config("watcher")
        source_seedfp = self._initialize_wallet("source", source_config, generate_mnemonic=True)
        self._initialize_wallet("watcher", watcher_config, generate_mnemonic=False)
        source = self._start_wallet("source", source_config)
        watcher = self._start_wallet("watcher", watcher_config)
        self._wait_wallet_tip(source, INITIAL_HEIGHT)
        self._wait_wallet_tip(watcher, INITIAL_HEIGHT)

        if source.z_listaccounts(False):
            raise RuntimeError("source wallet unexpectedly contained an offline-created account")
        if watcher.z_listaccounts(False):
            raise RuntimeError("watcher wallet unexpectedly contained an offline-created account")
        live_accounts = self._create_live_accounts(source, source_seedfp)
        self._wait_wallet_fully_synced(source, INITIAL_HEIGHT)
        viewing_key = live_accounts[0]["viewing_key"]
        try:
            imported = watcher.z_importviewingkey(viewing_key, "yes", 1)
        except Exception as error:
            raise RuntimeError(
                "native Zinder runtime did not complete non-genesis viewing-key "
                "import: {}".format(error)) from error
        if imported.get("address_type") != "sapling":
            raise RuntimeError("viewing-key import did not create a Sapling watch-only address")
        self._wait_wallet_fully_synced(watcher, INITIAL_HEIGHT)

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
        self._write_json("smoke-result.json", {
            "initial_height": INITIAL_HEIGHT,
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
                "creation_height": INITIAL_HEIGHT,
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
            "scope": "runtime smoke plus P2a history and steady-state expiry recovery",
        })

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

    def _staged_backend_pid(self, process_name: str) -> int:
        assert self.children is not None and self.runtime is not None
        child = self.children.children[process_name]
        expected = (
            self.runtime.root / "staged-zallet" / "zallet-zinder"
        ).resolve()
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

    def _write_wallet_config(self, name: str, recover_batch_size: int | None = None) -> Path:
        assert self.runtime is not None and self.evidence is not None
        wallet_root = self.runtime.root / "wallets" / name
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
            environment_overrides: dict[str, str] | None = None):
        assert self.children is not None
        if name == "source-restarted":
            process_name = "zallet-source-restarted"
        else:
            process_name = "zallet-" + name
        launcher = self._staged_launcher()
        environment = dict(os.environ)
        if environment_overrides is not None:
            environment.update(environment_overrides)
        child = self.children.start(
            process_name,
            [str(launcher), "--datadir={}".format(config.parent),
             "--config={}".format(config), "start"],
            environment)
        port = toml.load(config)["rpc"]["bind"][0].rsplit(":", 1)[1]
        endpoint = "http://zebra:zebra@127.0.0.1:{}".format(port)
        deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError("{} exited before JSON-RPC readiness".format(process_name))
            try:
                wallet = get_rpc_auth_proxy(endpoint, 100 if name.startswith("source") else 101, timeout=HTTP_TIMEOUT_SECONDS)
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
            self._write_json("manifest.json", {"result": "passed" if failure is None and cleanup_error is None else "failed",
                                                 "failure": None if failure is None else str(failure),
                                                 "cleanup_failure": None if cleanup_error is None else str(cleanup_error),
                                                 "scope": "runtime smoke plus P2a history and steady-state expiry recovery"})


if __name__ == '__main__':
    WalletZinderRecoverySmoke().main()
