#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

"""Certify Zallet's bounded scan against two real Zinder compositions."""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
import toml

from test_framework.config import ZebraArgs
from test_framework.proxy import JSONRPCException
from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import (
    bitcoind_processes,
    get_rpc_proxy,
    indexer_rpc_port,
    node_dir,
    p2p_port,
    rpc_port,
    rpc_url,
    update_zebrad_conf,
)


SCHEMA_VERSION = 1
NETWORK_NAME = "zcash-regtest"
INITIAL_SOURCE_HEIGHT = 19
ROTATED_SOURCE_HEIGHT = 20
REQUESTED_START_HEIGHT = 1
INITIAL_END_HEIGHT_EXCLUSIVE = 20
ROTATED_END_HEIGHT_EXCLUSIVE = 21
REORG_WINDOW_BLOCKS = 100
PROJECTOR_LEASE_DURATION_SECONDS = 14_400
READINESS_TIMEOUT_SECONDS = 240
CERTIFICATION_TIMEOUT_SECONDS = 240
ROTATION_BARRIER_TIMEOUT_SECONDS = 45
ZEBRA_SOURCE_READINESS_TIMEOUT_SECONDS = 120
ZEBRA_RPC_TIMEOUT_SECONDS = 5
PROCESS_TERMINATE_TIMEOUT_SECONDS = 10
PROCESS_KILL_TIMEOUT_SECONDS = 5
HTTP_REQUEST_TIMEOUT_SECONDS = 2
HTTP_POLL_INTERVAL_SECONDS = 0.1
PATH_POLL_INTERVAL_SECONDS = 0.05
PAIR_PUBLICATION_METRIC = (
    "zinder_wallet_serving_pair_publisher_publications_total"
)

INTEGRATION_TESTS_BASE_REVISION = (
    "054f21c33ed7c935248999c54b084451c314b5d5"
)
ZALLET_REVISION = "d03e30139d03f58b2dc8995f1742450438b07d53"
ZALLET_PAGINATOR_REVISION = "91694aef05c29f32b8cb7a1f486135e168e868fe"
ZALLET_CORE_CERTIFICATION_REVISION = (
    "61d2f86f179ced6b840e4bc0e839d6f8e04c5541"
)
ZINDER_RUNTIME_REVISION = "0b14b8846311794549b292af32cc1308a709b69c"
ZINDER_CLIENT_REVISION = "479805765d4c277115e534bf26f0a3ed6144bb73"
ZEBRA_REVISION = "7121c82ba6795a151523d5b308a19743f4a1ade7"

EXECUTABLE_ENVIRONMENTS = {
    "zebrad": "ZEBRAD",
    "zinder_ingest": "ZIT_ZINDER_INGEST",
    "zinder_projector": "ZIT_ZINDER_PROJECTOR",
    "zinder_query": "ZIT_ZINDER_QUERY",
    "zallet_zinder_test": "ZIT_ZALLET_ZINDER_TEST",
    "zallet_wallet_command": "ZIT_ZALLET",
}
EXPECTED_EXECUTABLE_SHA256 = {
    "zebrad": "4764c702e5b2dee3b8e6a67e5a76d333232a8da41b6c90b1b9cd106159ae97a9",
    "zinder_ingest": "e203210fa37c8ec54dcdb25783594c03302c053d0ef5a3e5c67f4aac0c754220",
    "zinder_projector": "92012f15f9a76311dba1ca55aed87d314927cb4b0194deb57abc3b4c4c074df1",
    "zinder_query": "de97270ab48f694f72bd4ab4fd4bc0418fb8e0b5691e03710aa60f2b2446efff",
    "zallet_zinder_test": "45f2469f61763d479cb2b9c95ce53e1585d1c508e8601f483d8f22aca77110e1",
    "zallet_wallet_command": "cff0a80f951044825d58ac1440d96cce960e046661ade6d6b20eb481ab10c17a",
}
EVIDENCE_DIRECTORY_ENVIRONMENT = "ZIT_ZINDER_EVIDENCE_DIR"

TRANSACTIONS_RETENTION_PREFLIGHT_REJECTION_TEST = (
    "chain::tests::endpoint_without_full_blocks_fails_before_wallet_open"
)
PAIR_ROTATION_REACQUISITION_TEST = (
    "chain::tests::expired_epoch_reacquires_complete_bounded_scan"
)
BIRTHDAY_THROUGH_TIP_SCAN_TEST = (
    "chain::tests::endpoint_certifies_birthday_through_tip"
)

TRANSACTIONS_RETENTION_PREFLIGHT_REJECTION = (
    "transactions_retention_preflight_rejection"
)
ALL_RETENTION_PAIR_ROTATION_REACQUISITION = (
    "all_retention_pair_rotation_reacquisition"
)
ALL_RETENTION_BIRTHDAY_THROUGH_TIP_SCAN = (
    "all_retention_birthday_through_tip_scan"
)
ALL_RETENTION_FRESH_PROCESS_RESTART = (
    "all_retention_fresh_process_restart"
)
EXPECTED_SCENARIO_IDENTIFIERS = frozenset({
    TRANSACTIONS_RETENTION_PREFLIGHT_REJECTION,
    ALL_RETENTION_PAIR_ROTATION_REACQUISITION,
    ALL_RETENTION_BIRTHDAY_THROUGH_TIP_SCAN,
    ALL_RETENTION_FRESH_PROCESS_RESTART,
})
TRANSACTIONS_PREFLIGHT_WALLET_DIRECTORY = (
    "wallet-transactions-retention-preflight-rejection"
)
ALL_PAIR_ROTATION_WALLET_DIRECTORY = (
    "wallet-all-retention-pair-rotation-reacquisition"
)
ALL_BOUNDED_SCAN_WALLET_DIRECTORY = (
    "wallet-all-retention-birthday-through-tip-scan"
)
EMBEDDED_RANGE_ATTEMPT_FIELDS = (
    "attempt_number",
    "chain_epoch_id",
    "requested_start_height_inclusive",
    "requested_end_height_inclusive",
)
RANGE_MARKER_FIELDS = frozenset(
    ("schema_version", *EMBEDDED_RANGE_ATTEMPT_FIELDS)
)

SERVER_INFO_CAPABILITY = "wallet.read.server_info_v2"
NETWORK_UPGRADE_CAPABILITY = "wallet.read.network_upgrade_activations_v1"
VISIBLE_TIP_CAPABILITY = "wallet.read.visible_tip_block_v1"
TREE_STATE_CAPABILITY = "wallet.read.tree_state_at_height_v2"
SUBTREE_ROOTS_CAPABILITY = "wallet.read.subtree_roots_in_range_v1"
IRONWOOD_SUBTREE_ROOTS_CAPABILITY = "wallet.read.subtree_roots_ironwood_v1"
FULL_BLOCK_CAPABILITY = "wallet.read.full_block_at_v1"
FULL_BLOCK_RANGE_CAPABILITY = "wallet.read.full_block_range_v1"
BOUNDED_SCAN_REQUIRED_CAPABILITIES = (
    SERVER_INFO_CAPABILITY,
    NETWORK_UPGRADE_CAPABILITY,
    VISIBLE_TIP_CAPABILITY,
    TREE_STATE_CAPABILITY,
    SUBTREE_ROOTS_CAPABILITY,
    IRONWOOD_SUBTREE_ROOTS_CAPABILITY,
    FULL_BLOCK_CAPABILITY,
    FULL_BLOCK_RANGE_CAPABILITY,
)
EXPECTED_MISSING_FULL_BLOCK_CAPABILITIES = (
    FULL_BLOCK_CAPABILITY,
    FULL_BLOCK_RANGE_CAPABILITY,
)

REGTEST_NUPARAMS = (
    "5ba81b19:1",
    "76b809bb:1",
    "2bb40e60:1",
    "f5b9230b:1",
    "e9ff75a6:1",
    "c2d6d0b4:1",
    "c8e71055:1",
    "4dec4df0:1",
    "5437f330:1",
)
BUILD_OWNER_HEX_BY_RETENTION = {
    "transactions": "10101010101010101010101010101010",
    "all": "20202020202020202020202020202020",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.parent / (
        f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    )
    descriptor = os.open(
        temporary_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())
        os.link(temporary_path, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary_path.unlink(missing_ok=True)


def atomic_write_text(path: Path, content: str) -> None:
    atomic_write_bytes(path, content.encode("utf8"))


def atomic_write_json(path: Path, document: object) -> None:
    encoded = json.dumps(document, indent=2, sort_keys=True) + "\n"
    atomic_write_text(path, encoded)


def atomic_copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination.parent / (
        f".{destination.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    )
    with source.open("rb") as source_file:
        descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as destination_file:
                shutil.copyfileobj(source_file, destination_file)
                destination_file.flush()
                os.fsync(destination_file.fileno())
            os.link(temporary_path, destination)
            directory_descriptor = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            temporary_path.unlink(missing_ok=True)


def require_absolute_executable(environment_name: str) -> Path:
    encoded_path = os.environ.get(environment_name)
    if encoded_path is None:
        raise RuntimeError(
            f"required environment variable {environment_name} is missing"
        )
    if not encoded_path:
        raise RuntimeError(
            f"required environment variable {environment_name} is empty"
        )
    executable_path = Path(encoded_path)
    if not executable_path.is_absolute():
        raise RuntimeError(
            f"{environment_name} must be an absolute path: {executable_path}"
        )
    if not executable_path.is_file():
        raise RuntimeError(
            f"{environment_name} is not an existing file: {executable_path}"
        )
    if not os.access(executable_path, os.X_OK):
        raise RuntimeError(
            f"{environment_name} is not executable: {executable_path}"
        )
    return executable_path


def require_frozen_executables() -> tuple[
    dict[str, Path],
    dict[str, str],
]:
    executables = {
        role: require_absolute_executable(environment_name)
        for role, environment_name in EXECUTABLE_ENVIRONMENTS.items()
    }
    executable_hashes = {
        role: sha256_file(path) for role, path in executables.items()
    }
    hash_mismatches = {
        role: {
            "expected": EXPECTED_EXECUTABLE_SHA256[role],
            "actual": executable_hash,
        }
        for role, executable_hash in executable_hashes.items()
        if executable_hash != EXPECTED_EXECUTABLE_SHA256[role]
    }
    if hash_mismatches:
        raise RuntimeError(
            "certification executable hashes differ from the frozen "
            f"Gate A artifacts: {hash_mismatches!r}"
        )
    return executables, executable_hashes


def require_evidence_directory() -> Path:
    encoded_path = os.environ.get(EVIDENCE_DIRECTORY_ENVIRONMENT)
    if encoded_path is None:
        raise RuntimeError(
            f"required environment variable "
            f"{EVIDENCE_DIRECTORY_ENVIRONMENT} is missing"
        )
    if not encoded_path:
        raise RuntimeError(
            f"required environment variable "
            f"{EVIDENCE_DIRECTORY_ENVIRONMENT} is empty"
        )
    evidence_directory = Path(encoded_path)
    if not evidence_directory.is_absolute():
        raise RuntimeError(
            f"{EVIDENCE_DIRECTORY_ENVIRONMENT} must be an absolute path: "
            f"{evidence_directory}"
        )
    if evidence_directory.exists():
        if not evidence_directory.is_dir():
            raise RuntimeError(
                f"{EVIDENCE_DIRECTORY_ENVIRONMENT} is not a directory: "
                f"{evidence_directory}"
            )
        if any(evidence_directory.iterdir()):
            raise RuntimeError(
                f"{EVIDENCE_DIRECTORY_ENVIRONMENT} must be empty: "
                f"{evidence_directory}"
            )
    else:
        evidence_directory.mkdir(parents=True, exist_ok=False)
    return evidence_directory


def require_clean_harness_source() -> tuple[str, Path]:
    source_path = Path(__file__).resolve()
    repository_root_result = subprocess.run(
        [
            "git",
            "-C",
            str(source_path.parent),
            "rev-parse",
            "--show-toplevel",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    repository_root_stderr = repository_root_result.stderr.strip()
    if (
        repository_root_result.returncode != 0
        or repository_root_stderr
    ):
        raise RuntimeError(
            "could not resolve the integration-tests repository root: "
            f"exit={repository_root_result.returncode}, "
            f"stderr={repository_root_stderr!r}"
        )
    repository_root = Path(
        repository_root_result.stdout.strip()
    ).resolve()
    try:
        relative_source_path = source_path.relative_to(repository_root)
    except ValueError as error:
        raise RuntimeError(
            f"executing harness source {source_path} is outside "
            f"repository root {repository_root}"
        ) from error
    expected_source_path = Path(
        "qa/rpc-tests/zinder_bounded_scan.py"
    )
    if relative_source_path != expected_source_path:
        raise RuntimeError(
            f"executing harness source is {relative_source_path}, expected "
            f"{expected_source_path}"
        )

    source_status_result = subprocess.run(
        [
            "git",
            "-C",
            str(repository_root),
            "status",
            "--porcelain",
            "--untracked-files=all",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    source_status_stderr = source_status_result.stderr.strip()
    if source_status_result.returncode != 0 or source_status_stderr:
        raise RuntimeError(
            "could not verify the harness source state: "
            f"exit={source_status_result.returncode}, "
            f"stderr={source_status_stderr!r}"
        )
    if source_status_result.stdout:
        raise RuntimeError(
            "bounded-scan certification requires the integration-tests "
            f"worktree to be clean: {source_status_result.stdout.strip()}"
        )
    revision_result = subprocess.run(
        ["git", "-C", str(repository_root), "rev-parse", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    revision_stderr = revision_result.stderr.strip()
    if revision_result.returncode != 0 or revision_stderr:
        raise RuntimeError(
            "could not resolve the integration-tests revision: "
            f"exit={revision_result.returncode}, "
            f"stderr={revision_stderr!r}"
        )
    revision = revision_result.stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise RuntimeError(
            f"integration-tests HEAD is not a full Git revision: "
            f"{revision!r}"
        )

    ancestry_result = subprocess.run(
        [
            "git",
            "-C",
            str(repository_root),
            "merge-base",
            "--is-ancestor",
            INTEGRATION_TESTS_BASE_REVISION,
            "HEAD",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    ancestry_stderr = ancestry_result.stderr.strip()
    if ancestry_result.returncode != 0 or ancestry_stderr:
        raise RuntimeError(
            "integration-tests HEAD does not descend from the frozen base: "
            f"exit={ancestry_result.returncode}, "
            f"stderr={ancestry_stderr!r}"
        )

    source_diff_result = subprocess.run(
        [
            "git",
            "-C",
            str(repository_root),
            "diff",
            "--name-status",
            "--no-renames",
            (
                f"{INTEGRATION_TESTS_BASE_REVISION}"
                "..HEAD"
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    source_diff_stderr = source_diff_result.stderr.strip()
    if source_diff_result.returncode != 0 or source_diff_stderr:
        raise RuntimeError(
            "could not verify the integration-tests source diff: "
            f"exit={source_diff_result.returncode}, "
            f"stderr={source_diff_stderr!r}"
        )
    expected_diff_line = f"A\t{expected_source_path.as_posix()}"
    if source_diff_result.stdout.splitlines() != [expected_diff_line]:
        raise RuntimeError(
            "integration-tests source diff must add only the bounded-scan "
            f"harness; observed={source_diff_result.stdout!r}"
        )
    return revision, source_path


def zinder_child_environment() -> dict[str, str]:
    return {
        name: setting
        for name, setting in os.environ.items()
        if not name.startswith("ZINDER_")
    }


def certification_child_environment() -> dict[str, str]:
    return {
        name: setting
        for name, setting in os.environ.items()
        if not name.startswith("ZIT_")
    }


def allocate_loopback_port(allocated_ports: set[int]) -> int:
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        if port not in allocated_ports:
            allocated_ports.add(port)
            return port


def wait_for_zebra_source_readiness(
    process: subprocess.Popen[bytes],
    json_rpc_endpoint: str,
    health_endpoint: str,
) -> object:
    deadline = (
        time.monotonic() + ZEBRA_SOURCE_READINESS_TIMEOUT_SECONDS
    )
    json_rpc_is_ready = False
    health_is_ready = False
    latest_json_rpc_failure = "endpoint has not responded"
    latest_health_failure = "endpoint has not responded"
    while True:
        if process.poll() is not None:
            raise RuntimeError(
                f"Zebra exited with {process.returncode} before source "
                "readiness"
            )
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            break

        if not json_rpc_is_ready:
            try:
                source_rpc = get_rpc_proxy(
                    json_rpc_endpoint,
                    0,
                    timeout=min(
                        ZEBRA_RPC_TIMEOUT_SECONDS,
                        remaining_seconds,
                    ),
                )
                source_rpc.getblockcount()
                json_rpc_is_ready = True
            except OSError as error:
                if error.errno != errno.ECONNREFUSED:
                    raise
                latest_json_rpc_failure = str(error)
            except JSONRPCException as error:
                error_code = error.error["code"]
                error_message = error.error["message"]
                if not (
                    error_code == -343
                    or (
                        error_code == -1
                        and error_message == "No blocks in state"
                    )
                ):
                    raise
                latest_json_rpc_failure = (
                    f"JSON-RPC {error_code}: {error_message}"
                )

        if process.poll() is not None:
            raise RuntimeError(
                f"Zebra exited with {process.returncode} before source "
                "readiness"
            )
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            break

        if not health_is_ready:
            try:
                try:
                    with urllib.request.urlopen(
                        health_endpoint,
                        timeout=min(
                            HTTP_REQUEST_TIMEOUT_SECONDS,
                            remaining_seconds,
                        ),
                    ) as response:
                        health_status_code = response.status
                        health_body = response.read().decode("utf8")
                except urllib.error.HTTPError as error:
                    health_status_code = error.code
                    health_body = error.read().decode("utf8")
                if health_status_code == 200 and health_body == "ok":
                    health_is_ready = True
                else:
                    latest_health_failure = (
                        f"HTTP {health_status_code}, body "
                        f"{health_body!r}"
                    )
            except (
                OSError,
                UnicodeDecodeError,
                urllib.error.URLError,
            ) as error:
                latest_health_failure = str(error)

        if process.poll() is not None:
            raise RuntimeError(
                f"Zebra exited with {process.returncode} before source "
                "readiness"
            )
        remaining_seconds = deadline - time.monotonic()
        if (
            json_rpc_is_ready
            and health_is_ready
            and remaining_seconds > 0
        ):
            return get_rpc_proxy(
                json_rpc_endpoint,
                0,
                timeout=ZEBRA_RPC_TIMEOUT_SECONDS,
            )
        if remaining_seconds <= 0:
            break
        time.sleep(min(0.25, remaining_seconds))
    raise RuntimeError(
        "Zebra did not become source-ready within "
        f"{ZEBRA_SOURCE_READINESS_TIMEOUT_SECONDS}s: JSON-RPC "
        f"{'ready' if json_rpc_is_ready else latest_json_rpc_failure}; "
        f"/ready {'ready' if health_is_ready else latest_health_failure}"
    )


def fetch_http_json(url: str) -> tuple[int, dict[str, object]]:
    try:
        with urllib.request.urlopen(
            url,
            timeout=HTTP_REQUEST_TIMEOUT_SECONDS,
        ) as response:
            encoded_body = response.read()
            status_code = response.status
    except urllib.error.HTTPError as error:
        encoded_body = error.read()
        status_code = error.code
    decoded_body = json.loads(encoded_body)
    if not isinstance(decoded_body, dict):
        raise RuntimeError(f"{url} returned a non-object JSON body")
    return status_code, decoded_body


def fetch_http_text(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(
            url,
            timeout=HTTP_REQUEST_TIMEOUT_SECONDS,
        ) as response:
            return response.status, response.read().decode("utf8")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf8")


def wait_for_health(
    ops_endpoint: str,
    service_name: str,
    child: subprocess.Popen[bytes],
    deadline: float | None = None,
) -> dict[str, object]:
    if deadline is None:
        deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
    latest_failure = "endpoint has not responded"
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError(
                f"{service_name} exited with {child.returncode} before /healthz"
            )
        try:
            status_code, health = fetch_http_json(
                f"{ops_endpoint}/healthz"
            )
            if status_code == 200 and health.get("status") == "alive":
                if health.get("service") != service_name:
                    raise RuntimeError(
                        f"{service_name} /healthz reported service "
                        f"{health.get('service')!r}"
                    )
                if health.get("network") != NETWORK_NAME:
                    raise RuntimeError(
                        f"{service_name} /healthz reported network "
                        f"{health.get('network')!r}"
                    )
                if health.get("git_commit") != ZINDER_RUNTIME_REVISION:
                    raise RuntimeError(
                        f"{service_name} binary reports git commit "
                        f"{health.get('git_commit')!r}, expected "
                        f"{ZINDER_RUNTIME_REVISION}"
                    )
                return health
            latest_failure = (
                f"HTTP {status_code}, status {health.get('status')!r}"
            )
        except (
            OSError,
            ValueError,
            urllib.error.URLError,
        ) as error:
            latest_failure = str(error)
        time.sleep(HTTP_POLL_INTERVAL_SECONDS)
    raise RuntimeError(
        f"{service_name} /healthz did not become alive before its deadline: "
        f"{latest_failure}"
    )


def wait_for_readiness(
    ops_endpoint: str,
    service_name: str,
    child: subprocess.Popen[bytes],
    minimum_height: int,
    deadline: float | None = None,
) -> dict[str, object]:
    if deadline is None:
        deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
    latest_failure = "endpoint has not responded"
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError(
                f"{service_name} exited with {child.returncode} before /readyz"
            )
        try:
            status_code, readiness = fetch_http_json(
                f"{ops_endpoint}/readyz"
            )
            current_height = readiness.get("current_height")
            is_minimum_height = (
                isinstance(current_height, int)
                and not isinstance(current_height, bool)
                and current_height >= minimum_height
            )
            if (
                status_code == 200
                and readiness.get("status") == "ready"
                and is_minimum_height
            ):
                return readiness
            latest_failure = (
                f"HTTP {status_code}, status "
                f"{readiness.get('status')!r}, current_height "
                f"{current_height!r}"
            )
        except (
            OSError,
            ValueError,
            urllib.error.URLError,
        ) as error:
            latest_failure = str(error)
        time.sleep(HTTP_POLL_INTERVAL_SECONDS)
    raise RuntimeError(
        f"{service_name} /readyz did not reach height {minimum_height} "
        f"before its deadline: {latest_failure}"
    )


def wait_for_file(
    path: Path,
    child: subprocess.Popen[bytes],
    description: str,
    deadline: float | None = None,
) -> None:
    if deadline is None:
        deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if path.is_file():
            return
        if path.exists():
            raise RuntimeError(f"{description} is not a file: {path}")
        if child.poll() is not None:
            raise RuntimeError(
                f"certification child exited with {child.returncode} before "
                f"{description}"
            )
        time.sleep(PATH_POLL_INTERVAL_SECONDS)
    raise RuntimeError(
        f"timed out waiting for {description}: {path}"
    )


def read_json_object(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf8") as source:
        document = json.load(source)
    if not isinstance(document, dict):
        raise RuntimeError(f"expected a JSON object in {path}")
    return document


def read_pair_publication_count(ops_endpoint: str) -> tuple[float, str]:
    status_code, metrics_text = fetch_http_text(f"{ops_endpoint}/metrics")
    if status_code != 200:
        raise RuntimeError(
            f"{ops_endpoint}/metrics returned HTTP {status_code}"
        )
    metric_series_pattern = re.compile(
        rf"^{re.escape(PAIR_PUBLICATION_METRIC)}(?:\{{|\s|$)"
    )
    metric_measurement_pattern = re.compile(
        rf"^{re.escape(PAIR_PUBLICATION_METRIC)}"
        r"(?:\{[^}]*\})?\s+([0-9eE+.-]+)$"
    )
    metric_series = [
        line
        for line in metrics_text.splitlines()
        if metric_series_pattern.match(line)
    ]
    if len(metric_series) != 1:
        raise RuntimeError(
            f"expected exactly one {PAIR_PUBLICATION_METRIC} measurement, "
            f"found {metric_series!r}"
        )
    measurement_match = metric_measurement_pattern.fullmatch(
        metric_series[0]
    )
    if measurement_match is None:
        raise RuntimeError(
            f"malformed {PAIR_PUBLICATION_METRIC} measurement: "
            f"{metric_series[0]!r}"
        )
    try:
        publication_count = float(measurement_match.group(1))
    except ValueError as error:
        raise RuntimeError(
            f"malformed {PAIR_PUBLICATION_METRIC} value: "
            f"{measurement_match.group(1)!r}"
        ) from error
    if not math.isfinite(publication_count):
        raise RuntimeError(
            f"non-finite {PAIR_PUBLICATION_METRIC} value: "
            f"{publication_count!r}"
        )
    return publication_count, metrics_text


def wait_for_pair_publication(
    ops_endpoint: str,
    child: subprocess.Popen[bytes],
    previous_publication_count: float,
    deadline: float | None = None,
) -> tuple[float, str]:
    if deadline is None:
        deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
    expected_publication_count = previous_publication_count + 1
    latest_publication_count = previous_publication_count
    latest_metrics = ""
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError(
                f"zinder-query exited with {child.returncode} before pair "
                "publication"
            )
        try:
            latest_publication_count, latest_metrics = (
                read_pair_publication_count(ops_endpoint)
            )
            if latest_publication_count == expected_publication_count:
                return latest_publication_count, latest_metrics
            if latest_publication_count != previous_publication_count:
                raise RuntimeError(
                    f"{PAIR_PUBLICATION_METRIC} changed from "
                    f"{previous_publication_count} to "
                    f"{latest_publication_count}, expected exactly "
                    f"{expected_publication_count}"
                )
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(HTTP_POLL_INTERVAL_SECONDS)
    raise RuntimeError(
        f"{PAIR_PUBLICATION_METRIC} did not reach "
        f"{expected_publication_count} from {previous_publication_count}; "
        f"last value was {latest_publication_count}"
    )


def require_rotation_barrier_budget(
    deadline: float,
    next_action: str,
) -> None:
    if time.monotonic() >= deadline:
        raise RuntimeError(
            f"rotation barrier exceeded "
            f"{ROTATION_BARRIER_TIMEOUT_SECONDS}s before {next_action}"
        )


@dataclass(frozen=True)
class ZinderComposition:
    retention: str
    root: Path
    canonical_primary: Path
    wallet_primary: Path
    config_paths: dict[str, Path]
    ops_endpoints: dict[str, str]
    ingest_control_endpoint: str
    query_endpoint: str


@dataclass
class CertificationChild:
    scenario_name: str
    child: subprocess.Popen[bytes]
    stdout_path: Path
    stderr_path: Path
    stdout_file: object
    stderr_file: object
    retained_logs: set[str] = field(default_factory=set)
    process_recorded: bool = False


class ZinderRuntimeProcesses:
    """Own one ingest, projector, and query composition until reverse shutdown."""

    def __init__(
        self,
        composition: ZinderComposition,
        executables: dict[str, Path],
        temporary_log_root: Path,
        evidence_directory: Path,
        process_records: list[dict[str, object]],
    ):
        self.composition = composition
        self.executables = executables
        self.temporary_log_root = temporary_log_root
        self.evidence_directory = evidence_directory
        self.process_records = process_records
        self.children: dict[str, subprocess.Popen[bytes]] = {}
        self.log_files: dict[str, tuple[object, object]] = {}
        self.log_paths: dict[str, tuple[Path, Path]] = {}
        self.start_order: list[str] = []
        self.shutdown_requested: set[str] = set()
        self.retained_logs: set[tuple[str, str]] = set()
        self.recorded_services: set[str] = set()

    def start(self, service_name: str) -> subprocess.Popen[bytes]:
        stdout_path = self.temporary_log_root / (
            f"{self.composition.retention}-{service_name}.stdout.log"
        )
        stderr_path = self.temporary_log_root / (
            f"{self.composition.retention}-{service_name}.stderr.log"
        )
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_file = stdout_path.open("xb")
        try:
            stderr_file = stderr_path.open("xb")
        except Exception:
            stdout_file.close()
            raise
        try:
            child = subprocess.Popen(
                [
                    self.executables[f"zinder_{service_name}"],
                    "--config",
                    self.composition.config_paths[service_name],
                ],
                env=zinder_child_environment(),
                stdout=stdout_file,
                stderr=stderr_file,
            )
        except Exception:
            stdout_file.close()
            stderr_file.close()
            raise
        self.children[service_name] = child
        self.log_files[service_name] = (stdout_file, stderr_file)
        self.log_paths[service_name] = (stdout_path, stderr_path)
        self.start_order.append(service_name)
        return child

    def child(self, service_name: str) -> subprocess.Popen[bytes]:
        return self.children[service_name]

    def stop(self) -> None:
        shutdown_errors = []
        services_to_stop = list(reversed(self.start_order))
        reaped_services = set()
        for service_name in services_to_stop:
            child = self.children[service_name]
            pre_shutdown_exit_code = child.poll()
            exited_before_shutdown = (
                pre_shutdown_exit_code is not None
                and service_name not in self.shutdown_requested
            )
            try:
                if pre_shutdown_exit_code is None:
                    self.shutdown_requested.add(service_name)
                    child.terminate()
                    try:
                        child.wait(
                            timeout=PROCESS_TERMINATE_TIMEOUT_SECONDS
                        )
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=PROCESS_KILL_TIMEOUT_SECONDS)
            except Exception as error:
                shutdown_errors.append(
                    f"{service_name} shutdown failed: {error}"
                )
            if child.poll() is None:
                continue
            reaped_services.add(service_name)
            if exited_before_shutdown:
                shutdown_errors.append(
                    f"{service_name} exited before controlled shutdown with "
                    f"{child.returncode}"
                )
            elif (
                service_name in self.shutdown_requested
                and child.returncode != 0
            ):
                shutdown_errors.append(
                    f"{service_name} controlled shutdown exited with "
                    f"{child.returncode}"
                )
            stdout_file, stderr_file = self.log_files[service_name]
            stdout_file.close()
            stderr_file.close()

        for service_name in services_to_stop:
            if service_name not in reaped_services:
                continue
            child = self.children[service_name]
            stdout_path, stderr_path = self.log_paths[service_name]
            log_directory = (
                self.evidence_directory
                / "services"
                / self.composition.retention
            )
            stdout_evidence_path = (
                log_directory / f"{service_name}.stdout.log"
            )
            stderr_evidence_path = (
                log_directory / f"{service_name}.stderr.log"
            )
            stdout_retention = (service_name, "stdout")
            stderr_retention = (service_name, "stderr")
            if stdout_retention not in self.retained_logs:
                try:
                    atomic_copy_file(stdout_path, stdout_evidence_path)
                    self.retained_logs.add(stdout_retention)
                except Exception as error:
                    shutdown_errors.append(
                        f"{service_name} stdout retention failed: {error}"
                    )
            if stderr_retention not in self.retained_logs:
                try:
                    atomic_copy_file(stderr_path, stderr_evidence_path)
                    self.retained_logs.add(stderr_retention)
                except Exception as error:
                    shutdown_errors.append(
                        f"{service_name} stderr retention failed: {error}"
                    )
            logs_retained = {
                stdout_retention,
                stderr_retention,
            }.issubset(self.retained_logs)
            if (
                logs_retained
                and service_name not in self.recorded_services
            ):
                self.process_records.append(
                    {
                        "kind": "zinder_runtime",
                        "composition": self.composition.retention,
                        "service": service_name,
                        "pid": child.pid,
                        "exit_code": child.returncode,
                    }
                )
                self.recorded_services.add(service_name)
        fully_recorded_services = (
            reaped_services & self.recorded_services
        )
        self.start_order = [
            service_name
            for service_name in self.start_order
            if service_name not in fully_recorded_services
        ]
        if shutdown_errors:
            raise RuntimeError("; ".join(shutdown_errors))


class ZinderBoundedScanTest(BitcoinTestFramework):
    def __init__(self):
        super().__init__()
        self.num_nodes = 1
        self.num_indexers = 0
        self.num_wallets = 0
        self.cache_behavior = "clean"
        self.executables: dict[str, Path] = {}
        self.executable_hashes: dict[str, str] = {}
        self.harness_revision = ""
        self.harness_source_path: Path | None = None
        self.harness_source_sha256 = ""
        self.zebra_process: subprocess.Popen[bytes] | None = None
        self.zebra_config_path: Path | None = None
        self.zebra_health_endpoint: str | None = None
        self.zebra_stdout_path: Path | None = None
        self.zebra_stderr_path: Path | None = None
        self.zebra_stdout_file = None
        self.zebra_stderr_file = None
        self.zebra_shutdown_requested = False
        self.zebra_retained_evidence: set[str] = set()
        self.zebra_process_recorded = False
        self.evidence_directory: Path | None = None
        self.runtime_root: Path | None = None
        self.process_records: list[dict[str, object]] = []
        self.scenario_summaries: dict[str, object] = {}
        self.wallet_artifact_hashes: dict[str, object] = {}
        self.active_runtime: ZinderRuntimeProcesses | None = None
        self.active_certification_children: list[CertificationChild] = []
        self.allocated_ports: set[int] = set()

    def setup_chain(self):
        framework_tmpdir = Path(self.options.tmpdir)
        if not framework_tmpdir.is_absolute():
            raise RuntimeError(
                f"framework tmpdir must be absolute: {framework_tmpdir}"
            )
        super().setup_chain()

    def setup_nodes(self):
        self.executables, self.executable_hashes = (
            require_frozen_executables()
        )
        (
            self.harness_revision,
            self.harness_source_path,
        ) = require_clean_harness_source()
        self.harness_source_sha256 = sha256_file(
            self.harness_source_path
        )
        self.allocated_ports.update(
            (rpc_port(0), p2p_port(0), indexer_rpc_port(0))
        )
        zebra_health_port = allocate_loopback_port(self.allocated_ports)
        zebra_health_listen_address = (
            f"127.0.0.1:{zebra_health_port}"
        )
        self.zebra_health_endpoint = (
            f"http://{zebra_health_listen_address}/ready"
        )
        zebra_data_directory = Path(node_dir(self.options.tmpdir, 0))
        self.zebra_config_path = Path(
            update_zebrad_conf(
                str(zebra_data_directory),
                rpc_port(0),
                p2p_port(0),
                indexer_rpc_port(0),
                ZebraArgs(activation_heights=self.activation_heights),
            )
        )
        with self.zebra_config_path.open(
            "r",
            encoding="utf8",
        ) as zebra_config_source:
            zebra_configuration = toml.load(zebra_config_source)
        zebra_configuration["health"] = {
            "listen_addr": zebra_health_listen_address,
            "enforce_on_test_networks": False,
        }
        with self.zebra_config_path.open(
            "w",
            encoding="utf8",
        ) as zebra_config_destination:
            toml.dump(zebra_configuration, zebra_config_destination)
        self.zebra_stdout_path = (
            Path(self.options.tmpdir) / "zebra.stdout.log"
        )
        self.zebra_stderr_path = (
            Path(self.options.tmpdir) / "zebra.stderr.log"
        )
        self.zebra_stdout_file = self.zebra_stdout_path.open("xb")
        try:
            self.zebra_stderr_file = self.zebra_stderr_path.open("xb")
        except Exception:
            self.zebra_stdout_file.close()
            raise
        try:
            self.zebra_process = subprocess.Popen(
                [
                    self.executables["zebrad"],
                    f"-c={self.zebra_config_path}",
                    "start",
                ],
                stdout=self.zebra_stdout_file,
                stderr=self.zebra_stderr_file,
            )
        except Exception:
            self.zebra_stdout_file.close()
            self.zebra_stderr_file.close()
            raise
        bitcoind_processes[0] = self.zebra_process
        node_endpoint = rpc_url(0)
        try:
            source_rpc = wait_for_zebra_source_readiness(
                self.zebra_process,
                node_endpoint,
                self.zebra_health_endpoint,
            )
        except Exception:
            if self.zebra_process.poll() is None:
                self.zebra_process.terminate()
                try:
                    self.zebra_process.wait(
                        timeout=PROCESS_TERMINATE_TIMEOUT_SECONDS
                    )
                except subprocess.TimeoutExpired:
                    self.zebra_process.kill()
                    self.zebra_process.wait(
                        timeout=PROCESS_KILL_TIMEOUT_SECONDS
                    )
            bitcoind_processes.pop(0, None)
            self.zebra_stdout_file.close()
            self.zebra_stderr_file.close()
            raise
        return [source_rpc]

    def prepare_chain(self):
        current_height = self.nodes[0].getblockcount()
        if current_height != 0:
            raise RuntimeError(
                "clean Regtest source must begin at height 0, found "
                f"{current_height}"
            )
        self.nodes[0].generate(INITIAL_SOURCE_HEIGHT)
        prepared_height = self.nodes[0].getblockcount()
        if prepared_height != INITIAL_SOURCE_HEIGHT:
            raise RuntimeError(
                f"source preparation reached {prepared_height}, expected "
                f"{INITIAL_SOURCE_HEIGHT}"
            )

    def run_test(self):
        execution_error: Exception | None = None
        cleanup_error: Exception | None = None
        completion_error: Exception | None = None
        manifest_error: Exception | None = None
        try:
            self._configure_run()
            self._certify_bounded_scan()
        except Exception as error:
            execution_error = error
        try:
            self._stop_active_children()
        except Exception as error:
            cleanup_error = error
            try:
                self._stop_active_children()
            except Exception as retry_error:
                cleanup_error = RuntimeError(
                    f"{error}; cleanup retry also failed: {retry_error}"
                )
        if execution_error is None and cleanup_error is None:
            try:
                self._validate_completed_bounded_scan_certification()
            except Exception as error:
                completion_error = error

        run_error = execution_error
        if cleanup_error is not None:
            if run_error is None:
                run_error = cleanup_error
            else:
                run_error = RuntimeError(
                    f"{run_error}; cleanup also failed: {cleanup_error}"
                )
        if completion_error is not None:
            run_error = completion_error
        try:
            if self.evidence_directory is not None:
                self._write_manifest(run_error)
        except Exception as error:
            manifest_error = error

        if run_error is not None:
            if manifest_error is not None:
                raise RuntimeError(
                    f"{run_error}; manifest write also failed: "
                    f"{manifest_error}"
                ) from run_error
            raise run_error
        if manifest_error is not None:
            raise manifest_error

    def _configure_run(self) -> None:
        current_executables, current_hashes = require_frozen_executables()
        if (
            current_executables != self.executables
            or current_hashes != self.executable_hashes
        ):
            raise RuntimeError(
                "frozen executable inputs changed after Zebra startup"
            )
        harness_revision, harness_source_path = require_clean_harness_source()
        current_harness_source_sha256 = sha256_file(harness_source_path)
        if (
            harness_revision != self.harness_revision
            or harness_source_path != self.harness_source_path
            or current_harness_source_sha256
            != self.harness_source_sha256
        ):
            raise RuntimeError(
                "integration-tests harness changed after Zebra startup"
            )
        framework_tmpdir = Path(self.options.tmpdir)
        if not framework_tmpdir.is_absolute():
            raise RuntimeError(
                f"framework tmpdir must be absolute: {framework_tmpdir}"
            )
        self.evidence_directory = require_evidence_directory()
        self.runtime_root = framework_tmpdir / "zinder-bounded-scan"
        if not self.runtime_root.is_absolute():
            raise RuntimeError(
                f"Zinder runtime root must be absolute: {self.runtime_root}"
            )
        self.runtime_root.mkdir(parents=True, exist_ok=False)

        current_zebrad = Path(os.environ["ZEBRAD"])
        if self.executables["zebrad"] != current_zebrad:
            raise RuntimeError(
                "framework ZEBRAD input changed while the scenario was starting"
            )

        executable_evidence = {
            role: {
                "path": str(path),
                "sha256": self.executable_hashes[role],
            }
            for role, path in self.executables.items()
        }
        atomic_write_json(
            self.evidence_directory / "executables.json",
            {
                "schema_version": SCHEMA_VERSION,
                "executables": executable_evidence,
                "harness": {
                    "base_revision": INTEGRATION_TESTS_BASE_REVISION,
                    "revision": self.harness_revision,
                    "source_path": str(harness_source_path),
                    "source_sha256": self.harness_source_sha256,
                },
            },
        )

    def _certify_bounded_scan(self) -> None:
        assert self.runtime_root is not None
        assert self.evidence_directory is not None
        assert self.zebra_health_endpoint is not None
        node_endpoint = self.nodes[0].url
        node_health_endpoint = self.zebra_health_endpoint
        source_hashes = {
            "0": self.nodes[0].getblockhash(0),
            str(INITIAL_SOURCE_HEIGHT): self.nodes[0].getblockhash(
                INITIAL_SOURCE_HEIGHT
            ),
        }

        wallet_root = self.runtime_root / "wallets"
        transactions_preflight_wallet = (
            wallet_root / TRANSACTIONS_PREFLIGHT_WALLET_DIRECTORY
        )
        all_pair_rotation_wallet = (
            wallet_root / ALL_PAIR_ROTATION_WALLET_DIRECTORY
        )
        all_bounded_scan_wallet = (
            wallet_root / ALL_BOUNDED_SCAN_WALLET_DIRECTORY
        )
        transactions_preflight_wallet.mkdir(
            parents=True,
            exist_ok=False,
        )
        all_pair_rotation_wallet.mkdir(parents=True, exist_ok=False)
        all_bounded_scan_wallet.mkdir(parents=True, exist_ok=False)

        transactions_preflight_config = self._write_zallet_config(
            transactions_preflight_wallet
        )
        all_pair_rotation_config = self._write_zallet_config(
            all_pair_rotation_wallet
        )
        all_bounded_scan_config = self._write_zallet_config(
            all_bounded_scan_wallet
        )
        self._initialize_wallet(
            all_pair_rotation_wallet,
            all_pair_rotation_config,
        )
        self._initialize_wallet(
            all_bounded_scan_wallet,
            all_bounded_scan_config,
        )

        transactions = self._create_zinder_composition(
            "transactions",
            node_endpoint,
            node_health_endpoint,
        )
        self.active_runtime = self._start_composition(
            transactions,
            INITIAL_SOURCE_HEIGHT,
        )
        transactions_health = self._capture_composition_state(
            transactions,
            INITIAL_SOURCE_HEIGHT,
        )
        self._validate_query_capabilities(
            transactions_health["query"],
            expects_full_blocks=False,
        )
        transactions_preflight_result_path = (
            self.evidence_directory
            / "certification"
            / TRANSACTIONS_RETENTION_PREFLIGHT_REJECTION
            / "result.json"
        )
        self._run_certification_test(
            TRANSACTIONS_RETENTION_PREFLIGHT_REJECTION,
            TRANSACTIONS_RETENTION_PREFLIGHT_REJECTION_TEST,
            transactions.query_endpoint,
            transactions_preflight_config,
            transactions_preflight_wallet,
            REQUESTED_START_HEIGHT,
            INITIAL_END_HEIGHT_EXCLUSIVE,
            transactions_preflight_result_path,
        )
        transactions_preflight_evidence = read_json_object(
            transactions_preflight_result_path
        )
        self._validate_transactions_retention_preflight_rejection_evidence(
            transactions_preflight_evidence,
            transactions_preflight_wallet,
        )
        self.scenario_summaries[
            TRANSACTIONS_RETENTION_PREFLIGHT_REJECTION
        ] = {
            "status": "passed",
            "requested_start_height_inclusive": (
                REQUESTED_START_HEIGHT
            ),
            "requested_end_height_exclusive": (
                INITIAL_END_HEIGHT_EXCLUSIVE
            ),
            "missing_capabilities": list(
                EXPECTED_MISSING_FULL_BLOCK_CAPABILITIES
            ),
            "wallet_artifacts_absent": True,
        }
        self.active_runtime.stop()
        self.active_runtime = None

        all_blocks = self._create_zinder_composition(
            "all",
            node_endpoint,
            node_health_endpoint,
        )
        self.active_runtime = self._start_composition(
            all_blocks,
            INITIAL_SOURCE_HEIGHT,
        )
        all_health_at_19 = self._capture_composition_state(
            all_blocks,
            INITIAL_SOURCE_HEIGHT,
        )
        self._validate_query_capabilities(
            all_health_at_19["query"],
            expects_full_blocks=True,
        )

        source_hashes[str(ROTATED_SOURCE_HEIGHT)] = (
            self._run_pair_rotation_reacquisition(
                all_blocks,
                all_pair_rotation_config,
                all_pair_rotation_wallet,
            )
        )
        all_source_hashes = {
            height: self.nodes[0].getblockhash(height)
            for height in range(REQUESTED_START_HEIGHT, ROTATED_SOURCE_HEIGHT + 1)
        }
        self._run_birthday_scan_and_fresh_process_restart(
            all_blocks,
            all_bounded_scan_config,
            all_bounded_scan_wallet,
            all_source_hashes,
        )
        self.active_runtime.stop()
        self.active_runtime = None

        atomic_write_json(
            self.evidence_directory / "source-block-hashes.json",
            {
                "schema_version": SCHEMA_VERSION,
                "hashes_by_height": source_hashes,
            },
        )

    def _write_zallet_config(self, wallet_directory: Path) -> Path:
        assert self.runtime_root is not None
        assert self.evidence_directory is not None
        default_config_path = (
            Path(__file__).resolve().parents[1]
            / "defaults"
            / "zallet"
            / "zallet.toml"
        )
        with default_config_path.open("r", encoding="utf8") as source:
            zallet_configuration = toml.load(source)
        zallet_configuration.pop("backend", None)
        zallet_configuration["consensus"] = {
            "network": "regtest",
            "regtest_nuparams": list(REGTEST_NUPARAMS),
        }
        encryption_identity_path = (
            wallet_directory / "encryption-identity.txt"
        )
        zallet_configuration.setdefault("keystore", {})[
            "encryption_identity"
        ] = str(encryption_identity_path)
        zallet_config_path = wallet_directory / "zallet.toml"
        atomic_write_text(
            zallet_config_path,
            toml.dumps(zallet_configuration),
        )
        atomic_copy_file(
            zallet_config_path,
            self.evidence_directory
            / "configs"
            / "wallets"
            / f"{wallet_directory.name}.redacted.toml",
        )
        return zallet_config_path

    def _initialize_wallet(
        self,
        wallet_directory: Path,
        zallet_config_path: Path,
    ) -> None:
        wallet_database_path = wallet_directory / "wallet.db"
        if wallet_database_path.exists():
            raise RuntimeError(
                f"wallet initializer refuses existing database "
                f"{wallet_database_path}"
            )
        wallet_command = self.executables["zallet_wallet_command"]
        command_prefix = [
            wallet_command,
            f"--datadir={wallet_directory}",
            f"--config={zallet_config_path}",
        ]
        commands = (
            ("generate-encryption-identity",),
            ("init-wallet-encryption",),
            ("generate-mnemonic",),
            ("regtest", "generate-account-and-miner-address"),
        )
        wallet_environment = dict(os.environ)
        wallet_environment.pop("ZALLET_IDENTITY_PASSPHRASE", None)
        for command_arguments in commands:
            completed = subprocess.run(
                [*command_prefix, *command_arguments],
                env=wallet_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=CERTIFICATION_TIMEOUT_SECONDS,
                check=False,
            )
            if completed.returncode != 0:
                command_name = " ".join(command_arguments)
                raise RuntimeError(
                    f"current Zallet wallet command {command_name!r} exited "
                    f"with {completed.returncode}; no command output was "
                    "retained because wallet setup may contain key identifiers"
                )
        if not wallet_database_path.is_file():
            raise RuntimeError(
                f"offline Zallet initialization did not create "
                f"{wallet_database_path}"
            )

    def _create_zinder_composition(
        self,
        retention: str,
        node_endpoint: str,
        node_health_endpoint: str,
    ) -> ZinderComposition:
        assert self.runtime_root is not None
        assert self.evidence_directory is not None
        composition_root = self.runtime_root / "compositions" / retention
        composition_root.mkdir(parents=True, exist_ok=False)
        canonical_primary = composition_root / "canonical-primary"
        projector_canonical_secondary = (
            composition_root / "projector-canonical-secondary"
        )
        wallet_primary = composition_root / "wallet-primary"
        query_canonical_secondary = (
            composition_root / "query-canonical-secondary"
        )
        query_wallet_secondary = (
            composition_root / "query-wallet-secondary"
        )
        checkpoint_staging_root = (
            composition_root / "checkpoint-staging"
        )
        checkpoint_staging_root.mkdir(parents=True, exist_ok=False)
        ports = [
            allocate_loopback_port(self.allocated_ports)
            for _ in range(5)
        ]
        (
            ingest_control_port,
            ingest_ops_port,
            projector_ops_port,
            query_port,
            query_ops_port,
        ) = ports
        ingest_control_address = f"127.0.0.1:{ingest_control_port}"
        ingest_control_endpoint = f"http://{ingest_control_address}"

        render_specs = {
            "ingest": (
                [
                    "--network",
                    NETWORK_NAME,
                    "--node-source",
                    "zebra-json-rpc",
                    "--json-rpc-addr",
                    node_endpoint,
                    "--node-auth-method",
                    "none",
                    "--storage-path",
                    str(canonical_primary),
                    "--reorg-window-blocks",
                    str(REORG_WINDOW_BLOCKS),
                    "--wallet-serving",
                    "--ingest-control-listen-addr",
                    ingest_control_address,
                    "--ops-listen-addr",
                    f"127.0.0.1:{ingest_ops_port}",
                ],
                {
                    "ZINDER_NODE__HEALTH__ADDR": node_health_endpoint,
                    "ZINDER_STORAGE__RAW_BLOB_POLICY": retention,
                    "ZINDER_INGEST_CONTROL__CHECKPOINT_STAGING_ROOT": str(
                        checkpoint_staging_root
                    ),
                },
            ),
            "projector": (
                [
                    "--network",
                    NETWORK_NAME,
                    "--canonical-path",
                    str(canonical_primary),
                    "--canonical-secondary-path",
                    str(projector_canonical_secondary),
                    "--wallet-path",
                    str(wallet_primary),
                    "--reorg-window-blocks",
                    str(REORG_WINDOW_BLOCKS),
                    "--build-owner-hex",
                    BUILD_OWNER_HEX_BY_RETENTION[retention],
                    "--lease-duration-seconds",
                    str(PROJECTOR_LEASE_DURATION_SECONDS),
                    "--node-json-rpc-addr",
                    node_endpoint,
                    "--ingest-control-addr",
                    ingest_control_endpoint,
                    "--ops-listen-addr",
                    f"127.0.0.1:{projector_ops_port}",
                ],
                {
                    "ZINDER_NODE__HEALTH__ADDR": node_health_endpoint,
                    "ZINDER_STORAGE__RAW_BLOB_POLICY": retention,
                    "ZINDER_PROJECTOR_CONTROL__CHECKPOINT_STAGING_ROOT": str(
                        checkpoint_staging_root
                    ),
                },
            ),
            "query": (
                [
                    "--network",
                    NETWORK_NAME,
                    "--canonical-primary-path",
                    str(canonical_primary),
                    "--canonical-secondary-root",
                    str(query_canonical_secondary),
                    "--raw-blob-policy",
                    retention,
                    "--wallet-primary-path",
                    str(wallet_primary),
                    "--wallet-secondary-root",
                    str(query_wallet_secondary),
                    "--ingest-control-addr",
                    ingest_control_endpoint,
                    "--listen-addr",
                    f"127.0.0.1:{query_port}",
                    "--reorg-window-blocks",
                    str(REORG_WINDOW_BLOCKS),
                    "--ops-listen-addr",
                    f"127.0.0.1:{query_ops_port}",
                    "--node-json-rpc-addr",
                    node_endpoint,
                ],
                {
                    "ZINDER_NODE__HEALTH__ADDR": node_health_endpoint,
                },
            ),
        }
        config_paths = {}
        for service_name, (arguments, render_environment) in render_specs.items():
            config_paths[service_name] = self._render_zinder_config(
                retention,
                service_name,
                arguments,
                render_environment,
            )

        composition = ZinderComposition(
            retention=retention,
            root=composition_root,
            canonical_primary=canonical_primary,
            wallet_primary=wallet_primary,
            config_paths=config_paths,
            ops_endpoints={
                "ingest": f"http://127.0.0.1:{ingest_ops_port}",
                "projector": f"http://127.0.0.1:{projector_ops_port}",
                "query": f"http://127.0.0.1:{query_ops_port}",
            },
            ingest_control_endpoint=ingest_control_endpoint,
            query_endpoint=f"http://127.0.0.1:{query_port}",
        )
        self._validate_composition_config(
            composition,
            node_endpoint,
            node_health_endpoint,
        )
        return composition

    def _render_zinder_config(
        self,
        retention: str,
        service_name: str,
        arguments: list[str],
        render_environment: dict[str, str],
    ) -> Path:
        assert self.runtime_root is not None
        assert self.evidence_directory is not None
        config_path = (
            self.runtime_root
            / "configs"
            / retention
            / f"{service_name}.toml"
        )
        environment = zinder_child_environment()
        environment.update(render_environment)
        completed = subprocess.run(
            [
                self.executables[f"zinder_{service_name}"],
                "--print-config",
                *arguments,
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=CERTIFICATION_TIMEOUT_SECONDS,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"{service_name} --print-config exited with "
                f"{completed.returncode}: "
                f"{completed.stderr.decode('utf8', errors='replace')}"
            )
        rendered_text = completed.stdout.decode("utf8")
        toml.loads(rendered_text)
        atomic_write_text(config_path, rendered_text)

        verified = subprocess.run(
            [
                self.executables[f"zinder_{service_name}"],
                "--print-config",
                "--config",
                config_path,
            ],
            env=zinder_child_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=CERTIFICATION_TIMEOUT_SECONDS,
            check=False,
        )
        if verified.returncode != 0:
            raise RuntimeError(
                f"self-contained {service_name} config was rejected with "
                f"{verified.returncode}: "
                f"{verified.stderr.decode('utf8', errors='replace')}"
            )
        if toml.loads(verified.stdout.decode("utf8")) != toml.loads(
            rendered_text
        ):
            raise RuntimeError(
                f"{service_name} rendered config changes after all "
                "ZINDER_* environment variables are stripped"
            )
        atomic_copy_file(
            config_path,
            self.evidence_directory
            / "configs"
            / retention
            / f"{service_name}.resolved.toml",
        )
        return config_path

    def _validate_composition_config(
        self,
        composition: ZinderComposition,
        node_endpoint: str,
        node_health_endpoint: str,
    ) -> None:
        loaded = {
            service_name: toml.load(config_path)
            for service_name, config_path in composition.config_paths.items()
        }
        expected_node_health = {
            "addr": node_health_endpoint,
            "poll_interval_ms": 30_000,
            "verification_progress_floor": 0.999,
            "estimated_gap_floor_blocks": 10,
        }
        for service_name, resolved in loaded.items():
            if resolved["network"]["name"] != NETWORK_NAME:
                raise RuntimeError(
                    f"{service_name} resolved the wrong network"
                )
            if resolved["node"]["json_rpc_addr"] != node_endpoint:
                raise RuntimeError(
                    f"{service_name} resolved the wrong Zebra endpoint"
                )
            if resolved["node"].get("health") != expected_node_health:
                raise RuntimeError(
                    f"{service_name} resolved node.health as "
                    f"{resolved['node'].get('health')!r}, expected "
                    f"{expected_node_health!r}"
                )
            if resolved["node"]["auth"]["method"] != "none":
                raise RuntimeError(
                    f"{service_name} did not resolve node auth method none"
                )
            if (
                resolved["storage"]["raw_blob_policy"]
                != composition.retention
            ):
                raise RuntimeError(
                    f"{service_name} resolved raw blob retention "
                    f"{resolved['storage']['raw_blob_policy']!r}, expected "
                    f"{composition.retention!r}"
                )
        if (
            loaded["ingest"]["ingest"]["run_overrides"]["coverage"]
            != "wallet-serving"
        ):
            raise RuntimeError(
                "ingest resolved config did not retain wallet-serving coverage"
            )
        for service_name in ("ingest", "projector", "query"):
            if loaded[service_name]["security"]["allow_public_bind"]:
                raise RuntimeError(
                    f"{service_name} unexpectedly permits public binds"
                )
        expected_checkpoint_staging_root = (
            composition.root / "checkpoint-staging"
        )
        for service_name, section_name in (
            ("ingest", "ingest_control"),
            ("projector", "projector_control"),
        ):
            rendered_checkpoint_staging_root = Path(
                loaded[service_name][section_name][
                    "checkpoint_staging_root"
                ]
            )
            if (
                rendered_checkpoint_staging_root
                != expected_checkpoint_staging_root
            ):
                raise RuntimeError(
                    f"{service_name} rendered checkpoint staging root "
                    f"{rendered_checkpoint_staging_root}, expected "
                    f"{expected_checkpoint_staging_root}"
                )
            if not rendered_checkpoint_staging_root.is_absolute():
                raise RuntimeError(
                    f"{service_name} rendered a relative checkpoint "
                    "staging root"
                )
        if (
            Path(loaded["ingest"]["storage"]["path"])
            != composition.canonical_primary
            or Path(loaded["projector"]["storage"]["canonical_path"])
            != composition.canonical_primary
            or Path(loaded["query"]["storage"]["path"])
            != composition.canonical_primary
        ):
            raise RuntimeError(
                f"{composition.retention} composition did not share its "
                "canonical primary"
            )
        if (
            Path(loaded["projector"]["wallet"]["path"])
            != composition.wallet_primary
            or Path(loaded["query"]["wallet"]["path"])
            != composition.wallet_primary
        ):
            raise RuntimeError(
                f"{composition.retention} composition did not share its "
                "wallet primary"
            )
        expected_secondary_roots = {
            ("projector", "storage", "canonical_secondary_path"): (
                composition.root / "projector-canonical-secondary"
            ),
            ("query", "storage", "secondary_path"): (
                composition.root / "query-canonical-secondary"
            ),
            ("query", "wallet", "secondary_path"): (
                composition.root / "query-wallet-secondary"
            ),
        }
        for keys, expected_root in expected_secondary_roots.items():
            service_name, section_name, field_name = keys
            rendered_root = Path(
                loaded[service_name][section_name][field_name]
            )
            if rendered_root != expected_root:
                raise RuntimeError(
                    f"{service_name} rendered {section_name}.{field_name} "
                    f"as {rendered_root}, expected {expected_root}"
                )
        for service_name, endpoint in composition.ops_endpoints.items():
            expected_listen_address = endpoint.removeprefix("http://")
            if (
                loaded[service_name]["ops"]["listen_addr"]
                != expected_listen_address
            ):
                raise RuntimeError(
                    f"{service_name} rendered the wrong ops listener"
                )
        expected_ingest_control_listener = (
            composition.ingest_control_endpoint.removeprefix("http://")
        )
        if (
            loaded["ingest"]["ingest_control"]["listen_addr"]
            != expected_ingest_control_listener
        ):
            raise RuntimeError(
                "ingest rendered the wrong control listener"
            )
        for service_name in ("projector", "query"):
            if (
                loaded[service_name]["ingest_control"]["addr"]
                != composition.ingest_control_endpoint
            ):
                raise RuntimeError(
                    f"{service_name} rendered the wrong ingest-control "
                    "endpoint"
                )
        if loaded["projector"]["projector_control"]["listen_addr"] != "":
            raise RuntimeError(
                "projector unexpectedly enabled its private control listener"
            )
        if (
            loaded["query"]["query"]["listen_addr"]
            != composition.query_endpoint.removeprefix("http://")
        ):
            raise RuntimeError("query rendered the wrong gRPC listener")

    def _start_composition(
        self,
        composition: ZinderComposition,
        minimum_height: int,
    ) -> ZinderRuntimeProcesses:
        assert self.runtime_root is not None
        assert self.evidence_directory is not None
        runtime = ZinderRuntimeProcesses(
            composition,
            self.executables,
            self.runtime_root / "logs",
            self.evidence_directory,
            self.process_records,
        )
        self.active_runtime = runtime
        for service_name in ("ingest", "projector", "query"):
            child = runtime.start(service_name)
            ops_endpoint = composition.ops_endpoints[service_name]
            wait_for_health(ops_endpoint, f"zinder-{service_name}", child)
            wait_for_readiness(
                ops_endpoint,
                f"zinder-{service_name}",
                child,
                minimum_height,
            )
        return runtime

    def _capture_composition_state(
        self,
        composition: ZinderComposition,
        minimum_height: int,
        deadline: float | None = None,
    ) -> dict[str, dict[str, object]]:
        assert self.evidence_directory is not None
        assert self.active_runtime is not None
        health_by_service = {}
        barrier_name = f"height-{minimum_height}"
        for service_name in ("ingest", "projector", "query"):
            child = self.active_runtime.child(service_name)
            ops_endpoint = composition.ops_endpoints[service_name]
            health = wait_for_health(
                ops_endpoint,
                f"zinder-{service_name}",
                child,
                deadline,
            )
            readiness = wait_for_readiness(
                ops_endpoint,
                f"zinder-{service_name}",
                child,
                minimum_height,
                deadline,
            )
            evidence_root = (
                self.evidence_directory
                / "ops"
                / composition.retention
                / barrier_name
            )
            atomic_write_json(
                evidence_root / f"{service_name}-health.json",
                health,
            )
            atomic_write_json(
                evidence_root / f"{service_name}-readiness.json",
                readiness,
            )
            health_by_service[service_name] = health
        atomic_write_json(
            self.evidence_directory
            / "ops"
            / composition.retention
            / barrier_name
            / "query-capabilities.json",
            {
                "schema_version": SCHEMA_VERSION,
                "capabilities": sorted(health_by_service["query"][
                    "capabilities"
                ]),
            },
        )
        return health_by_service

    def _validate_query_capabilities(
        self,
        health: dict[str, object],
        expects_full_blocks: bool,
    ) -> None:
        capabilities = health.get("capabilities")
        if not isinstance(capabilities, list) or not all(
            isinstance(capability, str) for capability in capabilities
        ):
            raise RuntimeError(
                "zinder-query /healthz capabilities are not a string list"
            )
        missing_required = [
            capability
            for capability in BOUNDED_SCAN_REQUIRED_CAPABILITIES
            if capability not in capabilities
        ]
        expected_missing = (
            []
            if expects_full_blocks
            else list(EXPECTED_MISSING_FULL_BLOCK_CAPABILITIES)
        )
        if missing_required != expected_missing:
            raise RuntimeError(
                f"zinder-query missing bounded-scan capabilities "
                f"{missing_required!r}, expected {expected_missing!r}"
            )

    def _run_certification_test(
        self,
        scenario_name: str,
        test_name: str,
        zinder_endpoint: str,
        zallet_config_path: Path,
        certification_datadir: Path,
        start_height: int,
        end_height_exclusive: int,
        certification_result_path: Path,
        retry_end_height_exclusive: int | None = None,
        barrier_directory: Path | None = None,
        range_request_pause_start_height: int | None = None,
        wait: bool = True,
    ) -> CertificationChild | None:
        assert self.runtime_root is not None
        assert self.evidence_directory is not None
        if certification_result_path.exists():
            raise RuntimeError(
                f"certification result already exists: "
                f"{certification_result_path}"
            )
        environment = certification_child_environment()
        environment.update(
            {
                "ZIT_ZINDER_ENDPOINT": zinder_endpoint,
                "ZIT_ZALLET_CONFIG": str(zallet_config_path),
                "ZIT_CERTIFICATION_DATADIR": str(
                    certification_datadir
                ),
                "ZIT_REQUESTED_START_HEIGHT": str(start_height),
                "ZIT_REQUESTED_END_HEIGHT_EXCLUSIVE": str(
                    end_height_exclusive
                ),
                "ZIT_CERTIFICATION_RESULT": str(
                    certification_result_path
                ),
            }
        )
        if retry_end_height_exclusive is not None:
            environment["ZIT_RETRY_END_HEIGHT_EXCLUSIVE"] = str(
                retry_end_height_exclusive
            )
        if barrier_directory is not None:
            environment["ZIT_RANGE_BARRIER_DIR"] = str(barrier_directory)
        if range_request_pause_start_height is not None:
            environment["ZIT_RANGE_REQUEST_PAUSE_START_HEIGHT"] = str(
                range_request_pause_start_height
            )

        temporary_log_root = self.runtime_root / "certification-logs"
        temporary_log_root.mkdir(parents=True, exist_ok=True)
        stdout_path = temporary_log_root / f"{scenario_name}.stdout.log"
        stderr_path = temporary_log_root / f"{scenario_name}.stderr.log"
        stdout_file = stdout_path.open("xb")
        try:
            stderr_file = stderr_path.open("xb")
        except Exception:
            stdout_file.close()
            raise
        try:
            child = subprocess.Popen(
                [
                    self.executables["zallet_zinder_test"],
                    test_name,
                    "--exact",
                    "--ignored",
                    "--nocapture",
                    "--test-threads=1",
                ],
                env=environment,
                stdout=stdout_file,
                stderr=stderr_file,
            )
        except Exception:
            stdout_file.close()
            stderr_file.close()
            raise
        certification_child = CertificationChild(
            scenario_name,
            child,
            stdout_path,
            stderr_path,
            stdout_file,
            stderr_file,
        )
        self.active_certification_children.append(certification_child)
        if wait:
            self._finish_certification_child(certification_child)
            return None
        return certification_child

    def _finish_certification_child(
        self,
        certification_child: CertificationChild,
    ) -> None:
        timed_out = False
        try:
            certification_child.child.wait(
                timeout=CERTIFICATION_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            certification_child.child.terminate()
            try:
                certification_child.child.wait(
                    timeout=PROCESS_TERMINATE_TIMEOUT_SECONDS
                )
            except subprocess.TimeoutExpired:
                certification_child.child.kill()
                certification_child.child.wait(
                    timeout=PROCESS_KILL_TIMEOUT_SECONDS
                )
        self._record_certification_child_exit(
            certification_child,
            cleanup=False,
        )
        if timed_out:
            raise RuntimeError(
                f"{certification_child.scenario_name} certification timed "
                f"out after {CERTIFICATION_TIMEOUT_SECONDS}s; inspect its "
                "retained stdout/stderr evidence"
            )
        if certification_child.child.returncode != 0:
            raise RuntimeError(
                f"{certification_child.scenario_name} certification exited "
                f"with {certification_child.child.returncode}; inspect its "
                "retained stdout/stderr evidence"
            )

    def _record_certification_child_exit(
        self,
        certification_child: CertificationChild,
        cleanup: bool,
    ) -> None:
        assert self.evidence_directory is not None
        certification_child.stdout_file.close()
        certification_child.stderr_file.close()
        evidence_log_root = (
            self.evidence_directory
            / "certification"
            / certification_child.scenario_name
        )
        stdout_evidence_path = evidence_log_root / "stdout.log"
        stderr_evidence_path = evidence_log_root / "stderr.log"
        if "stdout" not in certification_child.retained_logs:
            atomic_copy_file(
                certification_child.stdout_path,
                stdout_evidence_path,
            )
            certification_child.retained_logs.add("stdout")
        if "stderr" not in certification_child.retained_logs:
            atomic_copy_file(
                certification_child.stderr_path,
                stderr_evidence_path,
            )
            certification_child.retained_logs.add("stderr")
        if not certification_child.process_recorded:
            self.process_records.append(
                {
                    "kind": "zallet_certification",
                    "scenario": certification_child.scenario_name,
                    "pid": certification_child.child.pid,
                    "exit_code": certification_child.child.returncode,
                    "cleanup": cleanup,
                }
            )
            certification_child.process_recorded = True
        if certification_child.process_recorded:
            self.active_certification_children.remove(
                certification_child
            )

    def _validate_transactions_retention_preflight_rejection_evidence(
        self,
        evidence: dict[str, object],
        wallet_directory: Path,
    ) -> None:
        expected_wallet_artifacts = [
            str(wallet_directory / "wallet.db"),
            str(wallet_directory / "wallet.db-wal"),
            str(wallet_directory / "wallet.db-shm"),
        ]
        if evidence.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeError(
                "transactions-retention preflight evidence has the wrong "
                "schema"
            )
        if evidence.get("missing_capabilities") != list(
            EXPECTED_MISSING_FULL_BLOCK_CAPABILITIES
        ):
            raise RuntimeError(
                "transactions-retention preflight evidence does not "
                "contain the exact missing full-block capability pair"
            )
        if evidence.get("connection_rejected") is not True:
            raise RuntimeError(
                "transactions-retention preflight evidence did not prove "
                "connection rejection"
            )
        if evidence.get("wallet_artifact_paths") != expected_wallet_artifacts:
            raise RuntimeError(
                "transactions-retention preflight evidence names "
                "unexpected wallet artifacts"
            )
        for field_name in (
            "wallet_artifacts_absent_before_admission",
            "wallet_artifacts_absent_after_admission",
        ):
            if evidence.get(field_name) is not True:
                raise RuntimeError(
                    f"transactions-retention preflight field {field_name} "
                    "is not true"
                )
        for artifact_path in expected_wallet_artifacts:
            if Path(artifact_path).exists():
                raise RuntimeError(
                    f"transactions-retention preflight created wallet "
                    "artifact "
                    f"{artifact_path}"
                )

    def _run_pair_rotation_reacquisition(
        self,
        composition: ZinderComposition,
        zallet_config_path: Path,
        wallet_directory: Path,
    ) -> str:
        assert self.evidence_directory is not None
        assert self.active_runtime is not None
        scenario_root = (
            self.evidence_directory
            / "certification"
            / ALL_RETENTION_PAIR_ROTATION_REACQUISITION
        )
        result_path = scenario_root / "result.json"
        barrier_directory = scenario_root / "barrier"
        rotation_child = self._run_certification_test(
            ALL_RETENTION_PAIR_ROTATION_REACQUISITION,
            PAIR_ROTATION_REACQUISITION_TEST,
            composition.query_endpoint,
            zallet_config_path,
            wallet_directory,
            REQUESTED_START_HEIGHT,
            INITIAL_END_HEIGHT_EXCLUSIVE,
            result_path,
            retry_end_height_exclusive=ROTATED_END_HEIGHT_EXCLUSIVE,
            barrier_directory=barrier_directory,
            range_request_pause_start_height=REQUESTED_START_HEIGHT,
            wait=False,
        )
        assert rotation_child is not None
        rotation_deadline = (
            time.monotonic() + ROTATION_BARRIER_TIMEOUT_SECONDS
        )
        paused_path = barrier_directory / "range-request-paused.json"
        wait_for_file(
            paused_path,
            rotation_child.child,
            "range-request-paused marker",
            rotation_deadline,
        )
        require_rotation_barrier_budget(
            rotation_deadline,
            "checking the height-19 barrier",
        )
        if self.nodes[0].getblockcount() != INITIAL_SOURCE_HEIGHT:
            raise RuntimeError(
                "source advanced before the forced rotation barrier"
            )
        query_ops_endpoint = composition.ops_endpoints["query"]
        publication_before, metrics_before = read_pair_publication_count(
            query_ops_endpoint
        )
        if publication_before != 1:
            raise RuntimeError(
                f"pre-rotation {PAIR_PUBLICATION_METRIC} was "
                f"{publication_before}, expected exactly 1"
            )
        atomic_write_text(
            scenario_root / "metrics-before.txt",
            metrics_before,
        )
        require_rotation_barrier_budget(
            rotation_deadline,
            "mining the rotation block",
        )

        mined_hashes = self.nodes[0].generate(1)
        require_rotation_barrier_budget(
            rotation_deadline,
            "checking the rotated source",
        )
        if len(mined_hashes) != 1:
            raise RuntimeError(
                f"rotation mined {len(mined_hashes)} blocks, expected one"
            )
        if self.nodes[0].getblockcount() != ROTATED_SOURCE_HEIGHT:
            raise RuntimeError(
                "rotation did not advance source to height 20"
            )
        rotated_hash = self.nodes[0].getblockhash(ROTATED_SOURCE_HEIGHT)
        if mined_hashes[0] != rotated_hash:
            raise RuntimeError(
                "mined height-20 hash differs from Zebra getblockhash"
            )

        self._capture_composition_state(
            composition,
            ROTATED_SOURCE_HEIGHT,
            rotation_deadline,
        )
        require_rotation_barrier_budget(
            rotation_deadline,
            "waiting for strict pair publication",
        )
        publication_after, metrics_after = wait_for_pair_publication(
            query_ops_endpoint,
            self.active_runtime.child("query"),
            publication_before,
            rotation_deadline,
        )
        atomic_write_text(
            scenario_root / "metrics-after.txt",
            metrics_after,
        )
        atomic_write_json(
            scenario_root / "publisher-publications.json",
            {
                "schema_version": SCHEMA_VERSION,
                "metric": PAIR_PUBLICATION_METRIC,
                "before": publication_before,
                "after": publication_after,
            },
        )
        require_rotation_barrier_budget(
            rotation_deadline,
            "releasing the blocked range request",
        )
        atomic_write_json(
            barrier_directory / "continue-range-request",
            {
                "schema_version": SCHEMA_VERSION,
                "publication_before": publication_before,
                "publication_after": publication_after,
                "source_height": ROTATED_SOURCE_HEIGHT,
            },
        )
        require_rotation_barrier_budget(
            rotation_deadline,
            "finishing the rotation barrier",
        )
        self._finish_certification_child(rotation_child)
        rotation_evidence = read_json_object(result_path)
        attempt_markers = [
            self._validate_range_marker(
                barrier_directory / "range-request-attempt-1.json",
                attempt_number=1,
                start_height=REQUESTED_START_HEIGHT,
                end_height_inclusive=INITIAL_SOURCE_HEIGHT,
            ),
            self._validate_range_marker(
                barrier_directory / "range-request-attempt-2.json",
                attempt_number=2,
                start_height=REQUESTED_START_HEIGHT,
                end_height_inclusive=ROTATED_SOURCE_HEIGHT,
            ),
        ]
        paused_marker = read_json_object(paused_path)
        if paused_marker != attempt_markers[0]:
            raise RuntimeError(
                "range-request-paused marker differs from range attempt 1"
            )
        if (
            attempt_markers[0]["chain_epoch_id"]
            == attempt_markers[1]["chain_epoch_id"]
        ):
            raise RuntimeError(
                "rotation range markers reused the expired chain epoch"
            )
        projected_attempt_markers = [
            {
                field_name: marker[field_name]
                for field_name in EMBEDDED_RANGE_ATTEMPT_FIELDS
            }
            for marker in attempt_markers
        ]
        source_hashes = {
            height: self.nodes[0].getblockhash(height)
            for height in range(
                REQUESTED_START_HEIGHT,
                ROTATED_SOURCE_HEIGHT + 1,
            )
        }
        self._validate_pair_rotation_reacquisition_evidence(
            rotation_evidence,
            source_hashes,
            publication_before,
            publication_after,
            projected_attempt_markers,
        )
        integrity_evidence = self._record_sqlite_integrity(
            ALL_RETENTION_PAIR_ROTATION_REACQUISITION,
            wallet_directory,
        )
        self._record_wallet_artifacts(
            ALL_RETENTION_PAIR_ROTATION_REACQUISITION,
            wallet_directory,
        )
        self.scenario_summaries[
            ALL_RETENTION_PAIR_ROTATION_REACQUISITION
        ] = {
            "status": "passed",
            "expired_attempt_start_height_inclusive": (
                REQUESTED_START_HEIGHT
            ),
            "expired_attempt_end_height_exclusive": (
                INITIAL_END_HEIGHT_EXCLUSIVE
            ),
            "reacquired_attempt_start_height_inclusive": (
                REQUESTED_START_HEIGHT
            ),
            "reacquired_attempt_end_height_exclusive": (
                ROTATED_END_HEIGHT_EXCLUSIVE
            ),
            "publication_before": publication_before,
            "publication_after": publication_after,
            "sqlite_integrity_check": integrity_evidence[
                "integrity_check"
            ],
        }
        return rotated_hash

    def _run_birthday_scan_and_fresh_process_restart(
        self,
        composition: ZinderComposition,
        zallet_config_path: Path,
        wallet_directory: Path,
        source_hashes: dict[int, str],
    ) -> None:
        assert self.evidence_directory is not None
        birthday_scan_root = (
            self.evidence_directory
            / "certification"
            / ALL_RETENTION_BIRTHDAY_THROUGH_TIP_SCAN
        )
        birthday_scan_result_path = birthday_scan_root / "result.json"
        birthday_scan_barrier = birthday_scan_root / "barrier"
        self._run_certification_test(
            ALL_RETENTION_BIRTHDAY_THROUGH_TIP_SCAN,
            BIRTHDAY_THROUGH_TIP_SCAN_TEST,
            composition.query_endpoint,
            zallet_config_path,
            wallet_directory,
            REQUESTED_START_HEIGHT,
            ROTATED_END_HEIGHT_EXCLUSIVE,
            birthday_scan_result_path,
            barrier_directory=birthday_scan_barrier,
        )
        birthday_scan_evidence = read_json_object(
            birthday_scan_result_path
        )
        self._validate_birthday_through_tip_scan_evidence(
            birthday_scan_evidence,
            source_hashes,
            expected_metadata_before=[],
        )
        self._validate_range_marker(
            birthday_scan_barrier / "range-request-attempt-1.json",
            attempt_number=1,
            start_height=REQUESTED_START_HEIGHT,
            end_height_inclusive=ROTATED_SOURCE_HEIGHT,
        )
        birthday_scan_integrity = self._record_sqlite_integrity(
            ALL_RETENTION_BIRTHDAY_THROUGH_TIP_SCAN,
            wallet_directory,
        )
        self._record_wallet_artifacts(
            ALL_RETENTION_BIRTHDAY_THROUGH_TIP_SCAN,
            wallet_directory,
        )
        self.scenario_summaries[
            ALL_RETENTION_BIRTHDAY_THROUGH_TIP_SCAN
        ] = {
            "status": "passed",
            "requested_start_height_inclusive": (
                REQUESTED_START_HEIGHT
            ),
            "requested_end_height_exclusive": (
                ROTATED_END_HEIGHT_EXCLUSIVE
            ),
            "sqlite_integrity_check": birthday_scan_integrity[
                "integrity_check"
            ],
        }

        restart_root = (
            self.evidence_directory
            / "certification"
            / ALL_RETENTION_FRESH_PROCESS_RESTART
        )
        restart_result_path = restart_root / "result.json"
        restart_barrier = restart_root / "barrier"
        self._run_certification_test(
            ALL_RETENTION_FRESH_PROCESS_RESTART,
            BIRTHDAY_THROUGH_TIP_SCAN_TEST,
            composition.query_endpoint,
            zallet_config_path,
            wallet_directory,
            REQUESTED_START_HEIGHT,
            ROTATED_END_HEIGHT_EXCLUSIVE,
            restart_result_path,
            barrier_directory=restart_barrier,
        )
        restart_evidence = read_json_object(restart_result_path)
        self._validate_birthday_through_tip_scan_evidence(
            restart_evidence,
            source_hashes,
            expected_metadata_before=birthday_scan_evidence[
                "block_metadata_after"
            ],
        )
        if (
            restart_evidence["block_metadata_after"]
            != birthday_scan_evidence["block_metadata_after"]
        ):
            raise RuntimeError(
                "fresh-process restart changed certified block fingerprints"
            )
        self._validate_range_marker(
            restart_barrier / "range-request-attempt-1.json",
            attempt_number=1,
            start_height=REQUESTED_START_HEIGHT,
            end_height_inclusive=ROTATED_SOURCE_HEIGHT,
        )
        restart_integrity = self._record_sqlite_integrity(
            ALL_RETENTION_FRESH_PROCESS_RESTART,
            wallet_directory,
        )
        self._record_wallet_artifacts(
            ALL_RETENTION_FRESH_PROCESS_RESTART,
            wallet_directory,
        )
        self.scenario_summaries[
            ALL_RETENTION_FRESH_PROCESS_RESTART
        ] = {
            "status": "passed",
            "requested_start_height_inclusive": (
                REQUESTED_START_HEIGHT
            ),
            "requested_end_height_exclusive": (
                ROTATED_END_HEIGHT_EXCLUSIVE
            ),
            "same_wallet_datadir": True,
            "fresh_process": True,
            "sqlite_integrity_check": restart_integrity[
                "integrity_check"
            ],
        }

    def _validate_pair_rotation_reacquisition_evidence(
        self,
        evidence: dict[str, object],
        source_hashes: dict[int, str],
        publication_before: float,
        publication_after: float,
        projected_attempt_markers: list[dict[str, object]],
    ) -> None:
        if evidence.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeError("rotation evidence has the wrong schema")
        if (
            evidence.get("expiry_source_classification")
            != "IndexerError::ChainEpochPinUnavailable"
        ):
            raise RuntimeError(
                "rotation evidence lost typed ChainEpochPinUnavailable source"
            )
        expiry_evidence = evidence.get("expiry_evidence")
        if not isinstance(expiry_evidence, dict):
            raise RuntimeError("rotation expiry evidence is not an object")
        if expiry_evidence.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeError("rotation expiry evidence has the wrong schema")
        if (
            expiry_evidence.get("requested_start_height_inclusive")
            != REQUESTED_START_HEIGHT
            or expiry_evidence.get("requested_end_height_exclusive")
            != INITIAL_END_HEIGHT_EXCLUSIVE
            or expiry_evidence.get("wallet_birthday_height")
            != REQUESTED_START_HEIGHT
        ):
            raise RuntimeError(
                "rotation failed attempt records the wrong range or birthday"
            )
        metadata_before = expiry_evidence.get("block_metadata_before")
        metadata_after = expiry_evidence.get("block_metadata_after")
        if metadata_before != [] or metadata_after != metadata_before:
            raise RuntimeError(
                "expired range changed fresh-wallet block fingerprints"
            )
        if (
            expiry_evidence.get(
                "has_outstanding_scan_work_from_wallet_birthday_"
                "through_requested_last_height"
            )
            is not True
        ):
            raise RuntimeError(
                "expired attempt did not retain outstanding scan work"
            )

        attempts = evidence.get("range_request_attempts")
        if not isinstance(attempts, list) or len(attempts) != 2:
            raise RuntimeError(
                "rotation evidence does not contain two range attempts"
            )
        if attempts != projected_attempt_markers:
            raise RuntimeError(
                "rotation result attempts differ from projected barrier "
                "marker fields"
            )
        expected_ranges = ((1, 1, 19), (2, 1, 20))
        epoch_ids = []
        for attempt, expected in zip(attempts, expected_ranges):
            if not isinstance(attempt, dict):
                raise RuntimeError("rotation range attempt is not an object")
            expected_attempt, expected_start, expected_end = expected
            if (
                attempt.get("attempt_number") != expected_attempt
                or attempt.get("requested_start_height_inclusive")
                != expected_start
                or attempt.get("requested_end_height_inclusive")
                != expected_end
            ):
                raise RuntimeError(
                    f"rotation attempt differs from expected {expected!r}"
                )
            epoch_id = attempt.get("chain_epoch_id")
            if not isinstance(epoch_id, int) or isinstance(epoch_id, bool):
                raise RuntimeError(
                    "rotation attempt has an invalid chain epoch ID"
                )
            epoch_ids.append(epoch_id)
        if epoch_ids[0] == epoch_ids[1]:
            raise RuntimeError(
                "rotation retry reused the expired chain epoch"
            )
        if publication_before != 1:
            raise RuntimeError(
                "rotation did not begin after exactly one pair publication"
            )
        if publication_after != publication_before + 1:
            raise RuntimeError(
                "rotation did not observe exactly one pair-publication "
                "increment"
            )
        retry_evidence = evidence.get("retry_evidence")
        if not isinstance(retry_evidence, dict):
            raise RuntimeError("rotation retry evidence is not an object")
        self._validate_birthday_through_tip_scan_evidence(
            retry_evidence,
            source_hashes,
            expected_metadata_before=[],
        )

    def _validate_birthday_through_tip_scan_evidence(
        self,
        evidence: dict[str, object],
        source_hashes: dict[int, str],
        expected_metadata_before: object,
    ) -> None:
        if evidence.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeError("certified evidence has the wrong schema")
        expected_scalars = {
            "requested_start_height_inclusive": REQUESTED_START_HEIGHT,
            "requested_end_height_exclusive": ROTATED_END_HEIGHT_EXCLUSIVE,
            "wallet_birthday_height": REQUESTED_START_HEIGHT,
            "captured_tip_height": ROTATED_SOURCE_HEIGHT,
            "captured_tip_hash": source_hashes[ROTATED_SOURCE_HEIGHT],
            "has_outstanding_scan_work_from_wallet_birthday_"
            "through_captured_tip": False,
        }
        for field_name, expected in expected_scalars.items():
            if evidence.get(field_name) != expected:
                raise RuntimeError(
                    f"certified evidence field {field_name} is "
                    f"{evidence.get(field_name)!r}, expected {expected!r}"
                )
        if evidence.get("block_metadata_before") != expected_metadata_before:
            raise RuntimeError(
                "certified pre-scan fingerprints differ from expected wallet "
                "state"
            )
        self._validate_block_fingerprints(
            evidence.get("block_metadata_after"),
            source_hashes,
        )
        for field_name in (
            "sapling_subtree_root_count",
            "orchard_subtree_root_count",
            "ironwood_subtree_root_count",
        ):
            count = evidence.get(field_name)
            if (
                not isinstance(count, int)
                or isinstance(count, bool)
                or count < 0
            ):
                raise RuntimeError(
                    f"certified subtree-root count {field_name} is invalid"
                )

    def _validate_block_fingerprints(
        self,
        fingerprints: object,
        source_hashes: dict[int, str],
    ) -> None:
        if not isinstance(fingerprints, list):
            raise RuntimeError("block fingerprints are not a list")
        expected_heights = list(
            range(REQUESTED_START_HEIGHT, ROTATED_SOURCE_HEIGHT + 1)
        )
        observed_heights = []
        for fingerprint in fingerprints:
            if not isinstance(fingerprint, dict):
                raise RuntimeError("block fingerprint is not an object")
            height = fingerprint.get("block_height")
            if not isinstance(height, int) or isinstance(height, bool):
                raise RuntimeError("block fingerprint height is invalid")
            observed_heights.append(height)
            if fingerprint.get("block_hash") != source_hashes[height]:
                raise RuntimeError(
                    f"wallet fingerprint at height {height} differs from "
                    "Zebra's source hash"
                )
            for field_name in (
                "sapling_tree_size",
                "orchard_tree_size",
                "ironwood_tree_size",
            ):
                tree_size = fingerprint.get(field_name)
                if tree_size is not None and (
                    not isinstance(tree_size, int)
                    or isinstance(tree_size, bool)
                    or tree_size < 0
                ):
                    raise RuntimeError(
                        f"block fingerprint {field_name} is invalid at "
                        f"height {height}"
                    )
        if observed_heights != expected_heights:
            raise RuntimeError(
                f"wallet block fingerprints cover {observed_heights!r}, "
                f"expected {expected_heights!r}"
            )

    def _validate_range_marker(
        self,
        marker_path: Path,
        attempt_number: int,
        start_height: int,
        end_height_inclusive: int,
    ) -> dict[str, object]:
        marker = read_json_object(marker_path)
        if set(marker) != RANGE_MARKER_FIELDS:
            raise RuntimeError(
                f"range marker {marker_path} has keys "
                f"{sorted(marker)!r}, expected "
                f"{sorted(RANGE_MARKER_FIELDS)!r}"
            )
        expected_fields = {
            "schema_version": SCHEMA_VERSION,
            "attempt_number": attempt_number,
            "requested_start_height_inclusive": start_height,
            "requested_end_height_inclusive": end_height_inclusive,
        }
        for field_name, expected in expected_fields.items():
            if marker.get(field_name) != expected:
                raise RuntimeError(
                    f"range marker {marker_path} field {field_name} is "
                    f"{marker.get(field_name)!r}, expected {expected!r}"
                )
        epoch_id = marker.get("chain_epoch_id")
        if not isinstance(epoch_id, int) or isinstance(epoch_id, bool):
            raise RuntimeError(
                f"range marker {marker_path} has an invalid chain epoch ID"
            )
        return marker

    def _record_sqlite_integrity(
        self,
        scenario_name: str,
        wallet_directory: Path,
    ) -> dict[str, object]:
        assert self.evidence_directory is not None
        wallet_database_path = wallet_directory / "wallet.db"
        connection = sqlite3.connect(
            f"file:{wallet_database_path}?mode=ro",
            uri=True,
        )
        try:
            integrity_rows = [
                row[0]
                for row in connection.execute("PRAGMA integrity_check")
            ]
        finally:
            connection.close()
        if integrity_rows != ["ok"]:
            raise RuntimeError(
                f"{scenario_name} wallet integrity check returned "
                f"{integrity_rows!r}"
            )
        integrity_evidence = {
            "schema_version": SCHEMA_VERSION,
            "wallet_database_path": str(wallet_database_path),
            "integrity_check": integrity_rows,
        }
        atomic_write_json(
            self.evidence_directory
            / "certification"
            / scenario_name
            / "sqlite-integrity.json",
            integrity_evidence,
        )
        return integrity_evidence

    def _record_wallet_artifacts(
        self,
        scenario_name: str,
        wallet_directory: Path,
    ) -> None:
        wallet_database_path = wallet_directory / "wallet.db"
        artifact_paths = (
            wallet_database_path,
            Path(f"{wallet_database_path}-wal"),
            Path(f"{wallet_database_path}-shm"),
        )
        self.wallet_artifact_hashes[scenario_name] = {
            str(path): {
                "exists": path.is_file(),
                "sha256": sha256_file(path) if path.is_file() else None,
            }
            for path in artifact_paths
        }

    def _stop_zebra_and_retain_evidence(self) -> None:
        if self.zebra_process is None:
            return
        zebra_process = self.zebra_process
        shutdown_errors = []
        pre_shutdown_exit_code = zebra_process.poll()
        exited_before_shutdown = (
            pre_shutdown_exit_code is not None
            and not self.zebra_shutdown_requested
        )
        if pre_shutdown_exit_code is None:
            self.zebra_shutdown_requested = True
            try:
                if self.nodes:
                    self.nodes[0].stop()
            except Exception as error:
                shutdown_errors.append(
                    f"Zebra RPC shutdown failed: {error}"
                )
            try:
                zebra_process.wait(
                    timeout=PROCESS_TERMINATE_TIMEOUT_SECONDS
                )
            except subprocess.TimeoutExpired:
                shutdown_errors.append(
                    "Zebra did not stop within the graceful shutdown timeout"
                )
                zebra_process.terminate()
                try:
                    zebra_process.wait(
                        timeout=PROCESS_TERMINATE_TIMEOUT_SECONDS
                    )
                except subprocess.TimeoutExpired:
                    zebra_process.kill()
                    zebra_process.wait(
                        timeout=PROCESS_KILL_TIMEOUT_SECONDS
                    )
        if exited_before_shutdown:
            shutdown_errors.append(
                "Zebra exited before controlled shutdown with "
                f"{zebra_process.returncode}"
            )
        elif zebra_process.returncode != 0:
            shutdown_errors.append(
                "Zebra controlled shutdown exited with "
                f"{zebra_process.returncode}"
            )
        bitcoind_processes.pop(0, None)
        if self.nodes is not None:
            self.nodes.clear()
        if self.zebra_stdout_file is not None:
            self.zebra_stdout_file.close()
        if self.zebra_stderr_file is not None:
            self.zebra_stderr_file.close()

        if self.evidence_directory is not None:
            zebra_evidence_root = (
                self.evidence_directory / "services" / "zebra"
            )
            zebra_evidence_files = (
                (
                    "config",
                    self.zebra_config_path,
                    self.evidence_directory
                    / "configs"
                    / "zebra"
                    / "config.toml",
                ),
                (
                    "stdout",
                    self.zebra_stdout_path,
                    zebra_evidence_root / "stdout.log",
                ),
                (
                    "stderr",
                    self.zebra_stderr_path,
                    zebra_evidence_root / "stderr.log",
                ),
            )
            for evidence_name, source_path, evidence_path in (
                zebra_evidence_files
            ):
                if source_path is None:
                    shutdown_errors.append(
                        f"Zebra evidence source for {evidence_path.name} "
                        "was not recorded"
                    )
                elif evidence_name not in self.zebra_retained_evidence:
                    try:
                        atomic_copy_file(source_path, evidence_path)
                        self.zebra_retained_evidence.add(evidence_name)
                    except Exception as error:
                        shutdown_errors.append(
                            f"Zebra evidence retention failed for "
                            f"{evidence_path.name}: {error}"
                        )
        zebra_evidence_complete = (
            self.evidence_directory is None
            or self.zebra_retained_evidence
            == {"config", "stdout", "stderr"}
        )
        if zebra_evidence_complete and not self.zebra_process_recorded:
            self.process_records.append(
                {
                    "kind": "zebra_source",
                    "pid": zebra_process.pid,
                    "exit_code": zebra_process.returncode,
                }
            )
            self.zebra_process_recorded = True
        if self.zebra_process_recorded:
            self.zebra_process = None
        if shutdown_errors:
            raise RuntimeError("; ".join(shutdown_errors))

    def _stop_active_children(self) -> None:
        cleanup_errors = []
        for certification_child in list(
            reversed(self.active_certification_children)
        ):
            child = certification_child.child
            try:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(
                            timeout=PROCESS_TERMINATE_TIMEOUT_SECONDS
                        )
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=PROCESS_KILL_TIMEOUT_SECONDS)
                self._record_certification_child_exit(
                    certification_child,
                    cleanup=True,
                )
            except Exception as error:
                cleanup_errors.append(
                    f"{certification_child.scenario_name} cleanup failed: "
                    f"{error}"
                )
                if child.poll() is not None:
                    certification_child.stdout_file.close()
                    certification_child.stderr_file.close()
        if self.active_runtime is not None:
            runtime = self.active_runtime
            try:
                runtime.stop()
            except Exception as error:
                cleanup_errors.append(
                    f"Zinder runtime cleanup failed: {error}"
                )
            finally:
                if not runtime.start_order:
                    self.active_runtime = None
        try:
            self._stop_zebra_and_retain_evidence()
        except Exception as error:
            cleanup_errors.append(f"Zebra cleanup failed: {error}")
        if cleanup_errors:
            raise RuntimeError("; ".join(cleanup_errors))

    def _validate_completed_bounded_scan_certification(self) -> None:
        if self.evidence_directory is None or self.runtime_root is None:
            raise RuntimeError(
                "completed certification has no evidence or runtime root"
            )
        if set(self.scenario_summaries) != EXPECTED_SCENARIO_IDENTIFIERS:
            raise RuntimeError(
                "completed certification has unexpected scenario summaries: "
                f"{sorted(self.scenario_summaries)!r}"
            )
        for scenario_identifier, summary in (
            self.scenario_summaries.items()
        ):
            if (
                not isinstance(summary, dict)
                or summary.get("status") != "passed"
            ):
                raise RuntimeError(
                    f"scenario {scenario_identifier} did not complete with "
                    "status passed"
                )
        expected_summary_ranges = {
            TRANSACTIONS_RETENTION_PREFLIGHT_REJECTION: {
                "requested_start_height_inclusive": (
                    REQUESTED_START_HEIGHT
                ),
                "requested_end_height_exclusive": (
                    INITIAL_END_HEIGHT_EXCLUSIVE
                ),
            },
            ALL_RETENTION_PAIR_ROTATION_REACQUISITION: {
                "expired_attempt_start_height_inclusive": (
                    REQUESTED_START_HEIGHT
                ),
                "expired_attempt_end_height_exclusive": (
                    INITIAL_END_HEIGHT_EXCLUSIVE
                ),
                "reacquired_attempt_start_height_inclusive": (
                    REQUESTED_START_HEIGHT
                ),
                "reacquired_attempt_end_height_exclusive": (
                    ROTATED_END_HEIGHT_EXCLUSIVE
                ),
            },
            ALL_RETENTION_BIRTHDAY_THROUGH_TIP_SCAN: {
                "requested_start_height_inclusive": (
                    REQUESTED_START_HEIGHT
                ),
                "requested_end_height_exclusive": (
                    ROTATED_END_HEIGHT_EXCLUSIVE
                ),
            },
            ALL_RETENTION_FRESH_PROCESS_RESTART: {
                "requested_start_height_inclusive": (
                    REQUESTED_START_HEIGHT
                ),
                "requested_end_height_exclusive": (
                    ROTATED_END_HEIGHT_EXCLUSIVE
                ),
            },
        }
        for scenario_identifier, expected_range in (
            expected_summary_ranges.items()
        ):
            summary = self.scenario_summaries[scenario_identifier]
            for field_name, expected_value in expected_range.items():
                if summary.get(field_name) != expected_value:
                    raise RuntimeError(
                        f"scenario {scenario_identifier} field "
                        f"{field_name} is {summary.get(field_name)!r}, "
                        f"expected {expected_value!r}"
                    )

        expected_zinder_processes = {
            (retention, service_name)
            for retention in ("transactions", "all")
            for service_name in ("ingest", "projector", "query")
        }
        observed_zinder_processes = []
        observed_certification_processes = []
        observed_zebra_processes = 0
        for process_record in self.process_records:
            process_kind = process_record.get("kind")
            process_id = process_record.get("pid")
            if (
                not isinstance(process_id, int)
                or isinstance(process_id, bool)
                or process_id <= 0
            ):
                raise RuntimeError(
                    f"completed process record has invalid PID: "
                    f"{process_record!r}"
                )
            exit_code = process_record.get("exit_code")
            if (
                not isinstance(exit_code, int)
                or isinstance(exit_code, bool)
                or exit_code != 0
            ):
                raise RuntimeError(
                    f"completed process exited unsuccessfully: "
                    f"{process_record!r}"
                )
            if process_kind == "zinder_runtime":
                observed_zinder_processes.append(
                    (
                        process_record.get("composition"),
                        process_record.get("service"),
                    )
                )
            elif process_kind == "zallet_certification":
                if process_record.get("cleanup") is not False:
                    raise RuntimeError(
                        "successful certification process was recorded as "
                        f"cleanup: {process_record!r}"
                    )
                observed_certification_processes.append(
                    process_record.get("scenario")
                )
            elif process_kind == "zebra_source":
                observed_zebra_processes += 1
            else:
                raise RuntimeError(
                    f"completed process inventory has unexpected kind "
                    f"{process_kind!r}"
                )
        if (
            len(observed_zinder_processes)
            != len(expected_zinder_processes)
            or set(observed_zinder_processes)
            != expected_zinder_processes
        ):
            raise RuntimeError(
                "completed Zinder process inventory is not exact-once: "
                f"{observed_zinder_processes!r}"
            )
        if (
            len(observed_certification_processes)
            != len(EXPECTED_SCENARIO_IDENTIFIERS)
            or set(observed_certification_processes)
            != EXPECTED_SCENARIO_IDENTIFIERS
        ):
            raise RuntimeError(
                "completed Zallet certification process inventory is not "
                f"exact-once: {observed_certification_processes!r}"
            )
        if observed_zebra_processes != 1 or len(self.process_records) != 11:
            raise RuntimeError(
                "completed process inventory must contain six Zinder, four "
                "Zallet, and one Zebra process"
            )
        if (
            self.active_runtime is not None
            or self.active_certification_children
            or self.zebra_process is not None
            or bitcoind_processes
        ):
            raise RuntimeError(
                "completed certification still owns active processes"
            )

        expected_wallet_directories = {
            ALL_RETENTION_PAIR_ROTATION_REACQUISITION: (
                self.runtime_root
                / "wallets"
                / ALL_PAIR_ROTATION_WALLET_DIRECTORY
            ),
            ALL_RETENTION_BIRTHDAY_THROUGH_TIP_SCAN: (
                self.runtime_root
                / "wallets"
                / ALL_BOUNDED_SCAN_WALLET_DIRECTORY
            ),
            ALL_RETENTION_FRESH_PROCESS_RESTART: (
                self.runtime_root
                / "wallets"
                / ALL_BOUNDED_SCAN_WALLET_DIRECTORY
            ),
        }
        if (
            set(self.wallet_artifact_hashes)
            != set(expected_wallet_directories)
        ):
            raise RuntimeError(
                "completed wallet artifact snapshots have unexpected keys: "
                f"{sorted(self.wallet_artifact_hashes)!r}"
            )
        for scenario_identifier, wallet_directory in (
            expected_wallet_directories.items()
        ):
            wallet_database_path = wallet_directory / "wallet.db"
            expected_artifact_paths = {
                str(wallet_database_path),
                str(Path(f"{wallet_database_path}-wal")),
                str(Path(f"{wallet_database_path}-shm")),
            }
            artifact_snapshot = self.wallet_artifact_hashes[
                scenario_identifier
            ]
            if (
                not isinstance(artifact_snapshot, dict)
                or set(artifact_snapshot) != expected_artifact_paths
            ):
                raise RuntimeError(
                    f"wallet artifact snapshot {scenario_identifier} has "
                    "unexpected paths"
                )
            for artifact_path, artifact_record in (
                artifact_snapshot.items()
            ):
                if (
                    not isinstance(artifact_record, dict)
                    or set(artifact_record) != {"exists", "sha256"}
                    or not isinstance(
                        artifact_record["exists"],
                        bool,
                    )
                ):
                    raise RuntimeError(
                        f"wallet artifact record is malformed: "
                        f"{artifact_path}"
                    )
                artifact_exists = artifact_record["exists"]
                artifact_sha256 = artifact_record["sha256"]
                if artifact_path == str(wallet_database_path):
                    if (
                        artifact_exists is not True
                        or not isinstance(artifact_sha256, str)
                        or re.fullmatch(
                            r"[0-9a-f]{64}",
                            artifact_sha256,
                        )
                        is None
                    ):
                        raise RuntimeError(
                            f"wallet database snapshot is incomplete for "
                            f"{scenario_identifier}"
                        )
                elif artifact_exists:
                    if (
                        not isinstance(artifact_sha256, str)
                        or re.fullmatch(
                            r"[0-9a-f]{64}",
                            artifact_sha256,
                        )
                        is None
                    ):
                        raise RuntimeError(
                            f"present wallet sidecar has no SHA-256: "
                            f"{artifact_path}"
                        )
                elif artifact_sha256 is not None:
                    raise RuntimeError(
                        f"absent wallet sidecar has a SHA-256: "
                        f"{artifact_path}"
                    )

        current_executables, current_hashes = require_frozen_executables()
        if (
            current_executables != self.executables
            or current_hashes != self.executable_hashes
        ):
            raise RuntimeError(
                "frozen executable inputs changed before manifest creation"
            )
        current_revision, current_source_path = (
            require_clean_harness_source()
        )
        current_source_sha256 = sha256_file(current_source_path)
        if (
            current_revision != self.harness_revision
            or current_source_path != self.harness_source_path
            or current_source_sha256 != self.harness_source_sha256
        ):
            raise RuntimeError(
                "integration-tests harness changed before manifest creation"
            )
        executable_evidence = read_json_object(
            self.evidence_directory / "executables.json"
        )
        expected_executable_evidence = {
            role: {
                "path": str(path),
                "sha256": self.executable_hashes[role],
            }
            for role, path in self.executables.items()
        }
        if executable_evidence != {
            "schema_version": SCHEMA_VERSION,
            "executables": expected_executable_evidence,
            "harness": {
                "base_revision": INTEGRATION_TESTS_BASE_REVISION,
                "revision": self.harness_revision,
                "source_path": str(self.harness_source_path),
                "source_sha256": self.harness_source_sha256,
            },
        }:
            raise RuntimeError(
                "executable or harness provenance evidence changed"
            )

        expected_evidence_files = {
            Path("executables.json"),
            Path("source-block-hashes.json"),
            Path("configs/zebra/config.toml"),
            Path("services/zebra/stdout.log"),
            Path("services/zebra/stderr.log"),
        }
        for wallet_directory_name in (
            TRANSACTIONS_PREFLIGHT_WALLET_DIRECTORY,
            ALL_PAIR_ROTATION_WALLET_DIRECTORY,
            ALL_BOUNDED_SCAN_WALLET_DIRECTORY,
        ):
            expected_evidence_files.add(
                Path(
                    "configs/wallets/"
                    f"{wallet_directory_name}.redacted.toml"
                )
            )
        for retention in ("transactions", "all"):
            for service_name in ("ingest", "projector", "query"):
                expected_evidence_files.add(
                    Path(
                        f"configs/{retention}/"
                        f"{service_name}.resolved.toml"
                    )
                )
                expected_evidence_files.add(
                    Path(
                        f"services/{retention}/"
                        f"{service_name}.stdout.log"
                    )
                )
                expected_evidence_files.add(
                    Path(
                        f"services/{retention}/"
                        f"{service_name}.stderr.log"
                    )
                )
        for retention, source_height in (
            ("transactions", INITIAL_SOURCE_HEIGHT),
            ("all", INITIAL_SOURCE_HEIGHT),
            ("all", ROTATED_SOURCE_HEIGHT),
        ):
            ops_root = Path(
                f"ops/{retention}/height-{source_height}"
            )
            expected_evidence_files.add(
                ops_root / "query-capabilities.json"
            )
            for service_name in ("ingest", "projector", "query"):
                expected_evidence_files.add(
                    ops_root / f"{service_name}-health.json"
                )
                expected_evidence_files.add(
                    ops_root / f"{service_name}-readiness.json"
                )
        for scenario_identifier in EXPECTED_SCENARIO_IDENTIFIERS:
            scenario_root = Path(
                f"certification/{scenario_identifier}"
            )
            expected_evidence_files.update(
                {
                    scenario_root / "result.json",
                    scenario_root / "stdout.log",
                    scenario_root / "stderr.log",
                }
            )
        rotation_root = Path(
            "certification/"
            f"{ALL_RETENTION_PAIR_ROTATION_REACQUISITION}"
        )
        expected_evidence_files.update(
            {
                rotation_root / "sqlite-integrity.json",
                rotation_root / "metrics-before.txt",
                rotation_root / "metrics-after.txt",
                rotation_root / "publisher-publications.json",
                rotation_root / "barrier/range-request-paused.json",
                rotation_root / "barrier/continue-range-request",
                rotation_root / "barrier/range-request-attempt-1.json",
                rotation_root / "barrier/range-request-attempt-2.json",
            }
        )
        for scenario_identifier in (
            ALL_RETENTION_BIRTHDAY_THROUGH_TIP_SCAN,
            ALL_RETENTION_FRESH_PROCESS_RESTART,
        ):
            scenario_root = Path(
                f"certification/{scenario_identifier}"
            )
            expected_evidence_files.add(
                scenario_root / "sqlite-integrity.json"
            )
            expected_evidence_files.add(
                scenario_root
                / "barrier/range-request-attempt-1.json"
            )
        observed_evidence_files = {
            path.relative_to(self.evidence_directory)
            for path in self.evidence_directory.rglob("*")
            if path.is_file()
        }
        if observed_evidence_files != expected_evidence_files:
            missing_files = sorted(
                str(path)
                for path in (
                    expected_evidence_files - observed_evidence_files
                )
            )
            unexpected_files = sorted(
                str(path)
                for path in (
                    observed_evidence_files - expected_evidence_files
                )
            )
            raise RuntimeError(
                f"evidence inventory mismatch; missing={missing_files!r}, "
                f"unexpected={unexpected_files!r}"
            )

    def _write_manifest(self, execution_error: Exception | None) -> None:
        assert self.evidence_directory is not None
        manifest_path = self.evidence_directory / "manifest.json"
        file_hashes = {
            str(path.relative_to(self.evidence_directory)): sha256_file(path)
            for path in sorted(self.evidence_directory.rglob("*"))
            if path.is_file() and path != manifest_path
        }
        executable_hashes = read_json_object(
            self.evidence_directory / "executables.json"
        )["executables"]
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "status": "failed" if execution_error is not None else "passed",
            "failure": (
                f"{type(execution_error).__name__}: {execution_error}"
                if execution_error is not None
                else None
            ),
            "source_revisions": {
                "integration_tests": self.harness_revision,
                "integration_tests_base": (
                    INTEGRATION_TESTS_BASE_REVISION
                ),
                "zallet": ZALLET_REVISION,
                "zallet_paginator": ZALLET_PAGINATOR_REVISION,
                "zallet_core_certification": (
                    ZALLET_CORE_CERTIFICATION_REVISION
                ),
                "zinder_runtime": ZINDER_RUNTIME_REVISION,
                "zinder_client": ZINDER_CLIENT_REVISION,
                "zebra": ZEBRA_REVISION,
            },
            "executables": executable_hashes,
            "scenarios": self.scenario_summaries,
            "processes": self.process_records,
            "wallet_artifacts": self.wallet_artifact_hashes,
            "files": file_hashes,
        }
        atomic_write_json(manifest_path, manifest)


if __name__ == "__main__":
    ZinderBoundedScanTest().main()
