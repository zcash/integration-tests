#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

"""Smoke the current Zallet launcher against a fresh native Zinder runtime.

This is deliberately a small real-process smoke, not P2a certification.  It
does not exercise expiry barriers, transaction submission, transparent-history
correctness, or an Ironwood transaction producer.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
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
ZINDER_REVISION = "515b98b490575a87d8cd629d01551114f42a5735"
ZALLET_REVISION = "4016e5c2e75117efff52ef7398ba1c888a67f31e"
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
        self._initialize_wallet("source", source_config)
        self._initialize_wallet("watcher", watcher_config)
        source = self._start_wallet("source", source_config)
        watcher = self._start_wallet("watcher", watcher_config)
        self._wait_wallet_tip(source, INITIAL_HEIGHT)
        self._wait_wallet_tip(watcher, INITIAL_HEIGHT)

        source_account = source.z_listaccounts()[0]["account_uuid"]
        source_ua = source.z_getaddressforaccount(source_account, ["sapling"])["address"]
        source_sapling = source.z_listunifiedreceivers(source_ua)["sapling"]
        try:
            viewing_key = source.z_exportviewingkey(source_sapling)
        except Exception as error:
            raise RuntimeError(
                "native Zinder runtime did not complete the source viewing-key "
                "export needed for non-genesis import: {}".format(error)) from error
        try:
            imported = watcher.z_importviewingkey(viewing_key, "yes", 1)
        except Exception as error:
            raise RuntimeError(
                "native Zinder runtime did not complete non-genesis viewing-key "
                "import: {}".format(error)) from error
        if imported.get("address_type") != "sapling":
            raise RuntimeError("viewing-key import did not create a Sapling watch-only address")
        self._wait_wallet_tip(watcher, INITIAL_HEIGHT)

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
        self._wait_wallet_tip(source, advanced_tip)
        self._wait_wallet_tip(watcher, advanced_tip)
        self._wait_for_backend_trace(
            "zallet-source",
            "ending Zinder mempool follow after visible-tip transition")
        # Both wallet tips after the backend has reported its quiet
        # mempool-follow termination are the reacquired view at the mined tip.
        source_status = source.getwalletstatus()
        watcher_status = watcher.getwalletstatus()
        source.stop()
        source_exit = self.children.stop("zallet-source")
        if source_exit != 0:
            raise RuntimeError("source Zallet did not stop cleanly: {}".format(source_exit))
        source = self._start_wallet("source-restarted", source_config)
        self._wait_wallet_tip(source, advanced_tip)

        rescan = watcher.z_importviewingkey(viewing_key, "yes", 1)
        if rescan.get("address_type") != "sapling":
            raise RuntimeError("existing viewing-key rescan did not return a Sapling address")
        self._wait_wallet_tip(watcher, advanced_tip)
        self._write_json("smoke-result.json", {
            "initial_height": INITIAL_HEIGHT,
            "advanced_height": advanced_tip,
            "known_coinbase_transaction_id": known_transaction,
            "quiet_mempool_follow": {
                "backend_trace": "ending Zinder mempool follow after visible-tip transition",
                "source_wallet_tip_after_reacquisition": source_status.get("wallet_tip"),
                "watcher_wallet_tip_after_reacquisition": watcher_status.get("wallet_tip"),
            },
            "existing_viewing_key_rescan": "RPC accepted rescan=yes; this smoke does not certify rewind semantics",
            "scope": "runtime smoke only; not P2a certification",
        })

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

    def _write_wallet_config(self, name: str) -> Path:
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
        path = Path(config_path)
        path.write_text(toml.dumps(config), encoding="utf8")
        redacted = dict(config)
        redacted["indexer"] = {"validator_address": "redacted"}
        (self.evidence / ("zallet-" + name + ".toml")).write_text(toml.dumps(redacted), encoding="utf8")
        return path

    def _initialize_wallet(self, name: str, config: Path):
        # The test-framework default config supplies a disposable encryption
        # identity with the copied datadir, so generating another would rightly
        # fail rather than overwrite it.
        commands = (("init-wallet-encryption",), ("generate-mnemonic",),
                    ("regtest", "generate-account-and-miner-address"))
        for arguments in commands:
            completed = subprocess.run([self._staged_launcher(), "--datadir={}".format(config.parent), "--config={}".format(config), *arguments],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
            if completed.returncode != 0:
                raise RuntimeError("{} wallet initialization command {} failed: {}".format(name, " ".join(arguments), completed.stderr))
        if not (config.parent / "wallet.db").is_file():
            raise RuntimeError("{} wallet initialization did not create wallet.db".format(name))

    def _start_wallet(self, name: str, config: Path):
        assert self.children is not None
        if name == "source-restarted":
            process_name = "zallet-source-restarted"
        else:
            process_name = "zallet-" + name
        launcher = self._staged_launcher()
        child = self.children.start(process_name, [str(launcher), "--datadir={}".format(config.parent), "--config={}".format(config), "start"], dict(os.environ))
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
                                                 "scope": "runtime smoke only; P2a certification remains open"})


if __name__ == '__main__':
    WalletZinderRecoverySmoke().main()
