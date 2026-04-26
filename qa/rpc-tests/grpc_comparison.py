#!/usr/bin/env python3
# Copyright (c) 2025 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

"""
gRPC parity test: compare CompactTxStreamer responses from Zainod and Lightwalletd
backed by the same Zebrad node.

Mirrors the Rust test fixtures in client_rpc_test_fixtures, porting them to Python
so they run inside the existing BitcoinTestFramework CI pipeline.

Chain setup (zcashd mines; all blocks are submitted to Zebrad via submitblock):
  The fixture begins with 200 transparent coinbase blocks to the zcashd0
  wallet t-address (taddr), yielding 100 mature UTXOs by height 200. A second
  standalone wallet (zcashd1) follows the same chain and owns the Orchard
  account used for Orchard spends, matching the separation used by the working
  Orchard wallet tests.

  The shielded fixture range then appends:
  - t→Sapling funding via z_shieldcoinbase to sapling_ua0
  - extra t→Sapling funding via z_shieldcoinbase to sapling_ua_aux
  - Sapling→Orchard cross-pool funding into orchard_addr0
  - Sapling→Sapling
  - t→Orchard funding into orchard_ua_aux
  - Orchard→Orchard
  - Orchard→Sapling
  - Sapling→t
  - Orchard→t

Chain caching:
  After the first run the zcashd block data and chain metadata (addresses, txids,
  heights) are saved to qa/rpc-tests/cache/grpc_comparison/.  Subsequent runs
  restore the zcashd state and skip block generation entirely, saving the time
  spent on z_sendmany proof generation.  Pass --fresh to force a rebuild and
  overwrite the existing cache.

Methods tested (CompactTxStreamer service):
  GetLightdInfo, GetLatestBlock, GetBlock, GetBlockNullifiers,
  GetBlockRange, GetBlockRangeNullifiers,
  GetTransaction, GetTaddressTxids, GetTaddressBalance, GetTaddressBalanceStream,
  GetTreeState, GetLatestTreeState, GetSubtreeRoots,
  GetAddressUtxos, GetAddressUtxosStream

Not yet tested (require a wallet to submit mempool transactions):
  GetMempoolTx, GetMempoolStream
"""

import json
import os
import tarfile
import time
from difflib import unified_diff
from decimal import Decimal

import grpc
from google.protobuf import text_format

from test_framework.config import ZebraArgs
from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import (
    assert_equal,
    assert_true,
    get_coinbase_address,
    initialize_chain,
    persistent_cache_path,
    persistent_cache_exists,
    start_nodes,
    start_zcashd_node,
    stop_zcashd_node,
    tarfile_extractall,
    wait_and_assert_operationid_status,
    zaino_grpc_port,
)
from test_framework.zip317 import ZIP_317_FEE, conventional_fee
from test_framework.proto import (
    compact_formats_pb2,
    service_pb2,
    service_pb2_grpc,
)

_GRPC_CACHE_NAME = 'grpc_comparison'
_GRPC_STAGE1_CACHE_NAME = 'grpc_comparison_stage1'
_GRPC_ACTIVATION_HEIGHT = 2
_GRPC_CACHE_VERSION = 8  # Bump when cached metadata/state layout changes incompatibly.
_GRPC_STAGE1_HEIGHT = 202
_GRPC_T_TO_SAPLING_HEIGHT = 201
_GRPC_SAPLING_TO_ORCHARD_HEIGHT = 203
_GRPC_SAPLING_TO_SAPLING_HEIGHT = 204
_GRPC_T_TO_ORCHARD_HEIGHT = 205
_GRPC_ORCHARD_TO_ORCHARD_HEIGHT = 206
_GRPC_ORCHARD_TO_SAPLING_HEIGHT = 207
_GRPC_SAPLING_TO_T_HEIGHT = 208
_GRPC_ORCHARD_TO_T_HEIGHT = 209
_GRPC_ZCASHD_NUPARAMS = {
    '5ba81b19': 1,                        # Overwinter
    '76b809bb': 1,                        # Sapling
    '2bb40e60': 1,                        # Blossom
    'f5b9230b': 1,                        # Heartwood
    'e9ff75a6': 1,                        # Canopy
    'c2d6d0b4': _GRPC_ACTIVATION_HEIGHT,  # NU5
    'c8e71055': _GRPC_ACTIVATION_HEIGHT,  # NU6
}


def _skip_cached_runtime_files(tarinfo):
    """Exclude runtime-only files from cached datadirs."""
    basename = os.path.basename(tarinfo.name)
    if basename in (
        'debug.log',
        'db.log',
        'peers.dat',
        'mempool.dat',
        'fee_estimates.dat',
    ):
        return None
    if basename.endswith('.lock'):
        return None
    return tarinfo


def _submit_missing_blocks(src_node, dst_node):
    """Submit any blocks missing from dst_node using raw blocks from src_node."""
    dst_height = dst_node.getblockcount()
    src_height = src_node.getblockcount()
    for height in range(dst_height + 1, src_height + 1):
        raw_hex = src_node.getblock(str(height), 0)
        result = dst_node.submitblock(raw_hex)
        if result is not None:
            raise Exception("submitblock to zcashd failed at height %d: %s" % (height, result))


def _relay_raw_transaction(src_node, dst_node, txid):
    """Relay a raw transaction from one standalone node to another."""
    raw_hex = src_node.getrawtransaction(txid)
    relayed_txid = dst_node.sendrawtransaction(raw_hex, True)
    assert_equal(txid, relayed_txid)


def _write_checkpoint_file(node, max_height, path):
    """Write Zebra checkpoints from genesis through max_height to path."""
    with open(path, 'w', encoding='utf8') as f:
        for height in range(0, max_height + 1):
            f.write("%d %s\n" % (height, node.getblockhash(height)))


def _grpc_metadata_fields():
    """Metadata persisted alongside the cached fixture chain."""
    return (
        'taddr',
        'sapling_ua0', 'sapling_ua_aux', 'orchard_ua1', 'orchard_ua_aux',
        'sapling_addr0', 'sapling_addr1',
        'orchard_addr0', 'orchard_addr1', '_orchard_aux_addr',
        't_to_sapling_txid',        't_to_sapling_height',
        't_to_orchard_txid',        't_to_orchard_height',
        'sapling_to_sapling_txid',  'sapling_to_sapling_height',
        'orchard_to_orchard_txid',  'orchard_to_orchard_height',
        'sapling_to_orchard_txid',  'sapling_to_orchard_height',
        'orchard_to_sapling_txid',  'orchard_to_sapling_height',
        'sapling_to_t_txid',        'sapling_to_t_height',
        'orchard_to_t_txid',        'orchard_to_t_height',
    )


def _collect_stream(streaming_call):
    """Collect all messages from a server-streaming gRPC call into a list."""
    results = []
    for msg in streaming_call:
        results.append(msg)
    return results


def _strict_compact_block(block):
    """Return a CompactBlock exactly as provided by the implementation."""
    strict = compact_formats_pb2.CompactBlock()
    strict.CopyFrom(block)
    return strict


def _compact_tx_summary(tx):
    """Return a short one-line summary of a CompactTx for failure messages."""
    return (
        "index=%d txid=%s spends=%d outputs=%d actions=%d"
        % (tx.index, tx.txid.hex(), len(tx.spends), len(tx.outputs), len(tx.actions))
    )


def _protobuf_unified_diff(z_block, l_block, label, max_lines=200):
    """Render a unified diff for two protobuf messages."""
    z_text = text_format.MessageToString(z_block)
    l_text = text_format.MessageToString(l_block)
    diff_lines = list(unified_diff(
        z_text.splitlines(),
        l_text.splitlines(),
        fromfile="%s (Zainod)" % label,
        tofile="%s (Lightwalletd)" % label,
        lineterm="",
    ))
    if not diff_lines:
        return "  No unified diff available."
    if len(diff_lines) > max_lines:
        diff_lines = diff_lines[:max_lines] + ["... diff truncated after %d lines ..." % max_lines]
    return "\n".join(diff_lines)


def _compact_block_mismatch_message(label, z_block, l_block):
    """
    Summarize the first useful difference between two CompactBlocks.

    Keep this compact enough for CI logs while still pointing developers at the
    exact block and CompactTx entry that diverged.
    """
    lines = [
        "%s mismatch at height %d:" % (label, z_block.height),
        "  Zainod:       protoVersion=%d hash=%s prevHash=%s vtx=%d"
        % (z_block.protoVersion, z_block.hash.hex(), z_block.prevHash.hex(), len(z_block.vtx)),
        "  Lightwalletd: protoVersion=%d hash=%s prevHash=%s vtx=%d"
        % (l_block.protoVersion, l_block.hash.hex(), l_block.prevHash.hex(), len(l_block.vtx)),
    ]

    if z_block.hash != l_block.hash or z_block.prevHash != l_block.prevHash:
        lines.append("")
        lines.append(_protobuf_unified_diff(z_block, l_block, label))
        return "\n".join(lines)

    shared_len = min(len(z_block.vtx), len(l_block.vtx))
    for index in range(shared_len):
        z_tx = z_block.vtx[index]
        l_tx = l_block.vtx[index]
        if z_tx != l_tx:
            lines.extend([
                "  First differing CompactTx:",
                "    Zainod[%d]: %s" % (index, _compact_tx_summary(z_tx)),
                "    Lightwalletd[%d]: %s" % (index, _compact_tx_summary(l_tx)),
            ])
            lines.append("")
            lines.append(_protobuf_unified_diff(z_block, l_block, label))
            return "\n".join(lines)

    if len(z_block.vtx) != len(l_block.vtx):
        extra_side = "Zainod" if len(z_block.vtx) > len(l_block.vtx) else "Lightwalletd"
        extra_txs = z_block.vtx[shared_len:] if len(z_block.vtx) > len(l_block.vtx) else l_block.vtx[shared_len:]
        lines.append("  Extra CompactTx entries on %s:" % extra_side)
        for tx in extra_txs[:3]:
            lines.append("    %s" % _compact_tx_summary(tx))
        if len(extra_txs) > 3:
            lines.append("    ... %d more" % (len(extra_txs) - 3))
        lines.append("")
        lines.append(_protobuf_unified_diff(z_block, l_block, label))
        return "\n".join(lines)

    lines.append("  Blocks differ, but no shorter structured summary was found.")
    lines.append("")
    lines.append(_protobuf_unified_diff(z_block, l_block, label))
    return "\n".join(lines)


def _assert_compact_block_equal(label, z_block, l_block):
    """Assert two CompactBlocks are identical, with a readable unified diff on failure."""
    if z_block != l_block:
        raise AssertionError(_compact_block_mismatch_message(label, z_block, l_block))


class GrpcComparisonTest(BitcoinTestFramework):

    def __init__(self):
        super().__init__()
        self.num_nodes = 1
        self.num_indexers = 1        # Zainod
        self.num_lightwalletds = 1   # Lightwalletd
        self.num_wallets = 0
        self.cache_behavior = 'clean'

        # Populated in setup_network (or restored from cache); used by test methods.
        self.taddr = None               # coinbase t-address (blocks 1–200)
        self.txid = None                # coinbase txid of block 1 (for GetTransaction)
        self.sapling_ua0 = None         # Sapling source used for the Sapling→Orchard funding tx
        self.sapling_ua_aux = None      # Sapling source used for later Sapling spends
        self.orchard_ua1 = None         # account 1 UA (spent from Orchard-funded account)
        self.orchard_ua_aux = None      # account 2 UA (spent from later Orchard-funded account)
        self.sapling_addr0 = None       # bare Sapling receiver of account 0 (funded at block 202)
        self.sapling_addr1 = None       # bare Sapling receiver of account 2 (receives at blocks 204, 206)
        self.orchard_addr0 = None       # bare Orchard receiver of account 1 (funded at blocks 201, 203)
        self.orchard_addr1 = None       # bare Orchard receiver of account 3 (receives at blocks 205, 203)
        self._orchard_aux_addr = None   # Orchard receiver used for the t→Orchard case and later Orchard spends
        self.t_to_sapling_txid = None
        self.t_to_sapling_height = None
        self.t_to_orchard_txid = None
        self.t_to_orchard_height = None
        self.sapling_to_sapling_txid = None
        self.sapling_to_sapling_height = None
        self.orchard_to_orchard_txid = None
        self.orchard_to_orchard_height = None
        self.sapling_to_orchard_txid = None
        self.sapling_to_orchard_height = None
        self.orchard_to_sapling_txid = None
        self.orchard_to_sapling_height = None
        self.sapling_to_t_txid = None
        self.sapling_to_t_height = None
        self.orchard_to_t_txid = None
        self.orchard_to_t_height = None

        self._chain_loaded_from_cache = False
        self._stage1_loaded_from_cache = False
        self._zebra_checkpoints = None

    def add_options(self, parser):
        parser.add_option(
            "--fresh",
            dest="fresh",
            default=False,
            action="store_true",
            help=(
                "Discard the final cached chain state and rebuild it. "
                "The full cache lives at qa/rpc-tests/cache/%s/. "
                "A reusable stage-1 wallet cache may still be used to skip "
                "the slow initial Sapling funding setup." % _GRPC_CACHE_NAME
            ),
        )

    def setup_chain(self):
        """Restore the final cache, fall back to the reusable stage-1 cache, or start clean."""
        cache_path = persistent_cache_path(_GRPC_CACHE_NAME)
        if not self.options.fresh and persistent_cache_exists(_GRPC_CACHE_NAME):
            try:
                self._load_cached_metadata(cache_path)
                print("grpc_comparison: loading chain from cache (%s)" % cache_path)
                initialize_chain(self.options.tmpdir, self.num_nodes,
                                 self.options.cachedir, 'clean')
                self._restore_framework_cache(cache_path)
                self._chain_loaded_from_cache = True
            except (IOError, OSError, ValueError) as e:
                print("grpc_comparison: ignoring incompatible full cache: %s" % str(e))
                initialize_chain(self.options.tmpdir, self.num_nodes,
                                 self.options.cachedir, 'clean')
        else:
            initialize_chain(self.options.tmpdir, self.num_nodes,
                             self.options.cachedir, 'clean')
        stage1_cache_path = persistent_cache_path(_GRPC_STAGE1_CACHE_NAME)
        if (not self._chain_loaded_from_cache and
                persistent_cache_exists(_GRPC_STAGE1_CACHE_NAME)):
            try:
                self._load_cached_metadata(stage1_cache_path)
                print("grpc_comparison: loading stage-1 chain cache (%s)" % stage1_cache_path)
                self._restore_stage1_cache(stage1_cache_path)
                self._stage1_loaded_from_cache = True
            except (IOError, OSError, ValueError) as e:
                print("grpc_comparison: ignoring incompatible stage-1 cache: %s" % str(e))

    def _load_cached_metadata(self, cache_path):
        with open(os.path.join(cache_path, 'chain_metadata.json')) as f:
            meta = json.load(f)

        if meta.get('cache_version') != _GRPC_CACHE_VERSION:
            raise ValueError(
                "cache version mismatch for %s: found %r, expected %r"
                % (cache_path, meta.get('cache_version'), _GRPC_CACHE_VERSION)
            )

        for field in _grpc_metadata_fields():
            setattr(self, field, meta.get(field))

    def _write_cached_metadata(self, cache_path):
        meta = {field: getattr(self, field) for field in _grpc_metadata_fields()}
        meta['cache_version'] = _GRPC_CACHE_VERSION
        with open(os.path.join(cache_path, 'chain_metadata.json'), 'w') as f:
            json.dump(meta, f, indent=2)

    def _persist_framework_cache(self):
        cache_path = persistent_cache_path(_GRPC_CACHE_NAME)
        if os.path.isdir(cache_path):
            import shutil
            shutil.rmtree(cache_path)
        os.makedirs(cache_path)

        src = os.path.join(self.options.tmpdir, 'node0')

        with tarfile.open(os.path.join(cache_path, 'zebrad_state.tar.gz'), 'w:gz') as tf:
            tf.add(src, arcname='node0', filter=_skip_cached_runtime_files)

        self._write_cached_metadata(cache_path)

    def _restore_framework_cache(self, cache_path):
        with tarfile.open(os.path.join(cache_path, 'zebrad_state.tar.gz'), 'r:gz') as tf:
            tarfile_extractall(tf, self.options.tmpdir)

    def _restore_stage1_cache(self, cache_path):
        self._load_cached_metadata(cache_path)
        for index in range(2):
            with tarfile.open(os.path.join(cache_path, 'zcashd%d_state.tar.gz' % index), 'r:gz') as tf:
                tarfile_extractall(tf, self.options.tmpdir)

    def _start_build_nodes(self):
        return [
            start_zcashd_node(0, self.options.tmpdir, activation_heights=_GRPC_ZCASHD_NUPARAMS),
            start_zcashd_node(1, self.options.tmpdir, activation_heights=_GRPC_ZCASHD_NUPARAMS),
        ]

    def _wait_for_build_nodes_height(self, build_nodes, expected_height, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            heights = [node.getblockcount() for node in build_nodes]
            if heights == [expected_height] * len(build_nodes):
                return
            time.sleep(1)
        raise AssertionError(
            "standalone zcashd nodes did not reach height %d: %s"
            % (expected_height, heights)
        )

    def _restart_build_node(self, build_nodes, index):
        """Restart a standalone builder node so its wallet reloads note state from disk."""
        stop_zcashd_node(index, build_nodes[index])
        build_nodes[index] = start_zcashd_node(
            index,
            self.options.tmpdir,
            activation_heights=_GRPC_ZCASHD_NUPARAMS,
        )
        return build_nodes[index]

    def _mine_and_sync_build_nodes(self, miner, build_nodes):
        """Mine one block on the canonical builder node and submit it to the follower node."""
        miner.generate(1)
        _submit_missing_blocks(build_nodes[0], build_nodes[1])
        return build_nodes[0].getblockcount()

    def _persist_stage1_cache(self, build_nodes):
        print("grpc_comparison: persisting stage-1 wallet cache")
        for index, node in enumerate(build_nodes):
            stop_zcashd_node(index, node)

        cache_path = persistent_cache_path(_GRPC_STAGE1_CACHE_NAME)
        if os.path.isdir(cache_path):
            import shutil
            shutil.rmtree(cache_path)
        os.makedirs(cache_path)

        for index in range(2):
            src = os.path.join(self.options.tmpdir, 'zcashd%d' % index)
            with tarfile.open(os.path.join(cache_path, 'zcashd%d_state.tar.gz' % index), 'w:gz') as tf:
                tf.add(src, arcname='zcashd%d' % index, filter=_skip_cached_runtime_files)

        self._write_cached_metadata(cache_path)
        build_nodes = self._start_build_nodes()
        self._wait_for_build_nodes_height(build_nodes, _GRPC_STAGE1_HEIGHT)
        return build_nodes

    def _build_stage1_with_wallet_nodes(self):
        """
        Build the reusable prefix of the fixture chain.

        Stage 1 does the slow work: mine 200 transparent blocks and create two
        independent Sapling note pools. Subsequent reruns can resume from this
        point without repeating the expensive proof generation.
        """
        build_nodes = self._start_build_nodes()
        node0, node1 = build_nodes

        assert_equal(node0.getblockcount(), 0)
        print("grpc_comparison: mining initial transparent chain (200 blocks)")
        node0.generate(200)
        _submit_missing_blocks(node0, node1)

        print("grpc_comparison: deriving Sapling and Orchard fixture addresses")
        self.taddr = get_coinbase_address(node0)
        self.txid = node0.getblock("1")['tx'][0]

        self.sapling_ua0 = node0.z_getnewaddress('sapling')
        self.sapling_addr0 = self.sapling_ua0

        self.sapling_ua_aux = node0.z_getnewaddress('sapling')
        self.sapling_addr1 = node0.z_getnewaddress('sapling')

        orchard_acct1 = node1.z_getnewaccount()['account']
        self.orchard_ua1 = node1.z_getaddressforaccount(orchard_acct1, ['orchard'])['address']
        self.orchard_addr0 = node1.z_listunifiedreceivers(self.orchard_ua1)['orchard']

        orchard_aux_acct = node1.z_getnewaccount()['account']
        self.orchard_ua_aux = node1.z_getaddressforaccount(orchard_aux_acct, ['orchard'])['address']
        self._orchard_aux_addr = node1.z_listunifiedreceivers(self.orchard_ua_aux)['orchard']

        orchard_acct2 = node1.z_getnewaccount()['account']
        orchard_ua2 = node1.z_getaddressforaccount(orchard_acct2, ['orchard'])['address']
        self.orchard_addr1 = node1.z_listunifiedreceivers(orchard_ua2)['orchard']

        print("grpc_comparison: funding primary Sapling pool from transparent coinbase")
        sapling_shield_fee = conventional_fee(4)
        sapling_shield_amount = Decimal('12.5') - sapling_shield_fee
        self.t_to_sapling_txid = wait_and_assert_operationid_status(
            node0,
            node0.z_sendmany(
                self.taddr,
                [{"address": self.sapling_ua0, "amount": sapling_shield_amount}],
                10,
                sapling_shield_fee,
                'AllowRevealedSenders',
            ),
        )
        self.t_to_sapling_height = self._mine_and_sync_build_nodes(node0, build_nodes)
        assert_equal(self.t_to_sapling_height, _GRPC_T_TO_SAPLING_HEIGHT)

        print("grpc_comparison: restarting primary builder wallet before auxiliary Sapling funding")
        node0 = self._restart_build_node(build_nodes, 0)
        assert_equal(node0.getblockcount(), self.t_to_sapling_height)

        print("grpc_comparison: funding auxiliary Sapling pool from transparent coinbase")
        wait_and_assert_operationid_status(
            node0,
            node0.z_sendmany(
                self.taddr,
                [{"address": self.sapling_ua_aux, "amount": sapling_shield_amount}],
                10,
                sapling_shield_fee,
                'AllowRevealedSenders',
            ),
        )
        assert_equal(self._mine_and_sync_build_nodes(node0, build_nodes), _GRPC_STAGE1_HEIGHT)

        return build_nodes

    def _complete_chain_from_stage1(self, build_nodes):
        """Build the shielded transaction range used by the parity assertions."""
        node0, node1 = build_nodes
        assert_equal(node0.getblockcount(), _GRPC_STAGE1_HEIGHT)
        _submit_missing_blocks(node0, node1)
        assert_equal(node1.getblockcount(), _GRPC_STAGE1_HEIGHT)

        fund = Decimal('0.1')
        amount = Decimal('0.01')
        sapling_to_orchard_fee = conventional_fee(4)

        print("grpc_comparison: building Sapling -> Orchard funding transaction")
        self.sapling_to_orchard_txid = wait_and_assert_operationid_status(
            node0,
            node0.z_sendmany(
                self.sapling_ua0,
                [{"address": self.orchard_addr0, "amount": fund}],
                1,
                sapling_to_orchard_fee,
                'AllowRevealedAmounts',
            ),
        )
        self.sapling_to_orchard_height = self._mine_and_sync_build_nodes(node0, build_nodes)
        assert_equal(self.sapling_to_orchard_height, _GRPC_SAPLING_TO_ORCHARD_HEIGHT)

        print("grpc_comparison: building Sapling -> Sapling transaction")
        self.sapling_to_sapling_txid = wait_and_assert_operationid_status(
            node0,
            node0.z_sendmany(
                self.sapling_ua_aux,
                [{"address": self.sapling_addr1, "amount": amount}],
                1,
                ZIP_317_FEE,
            ),
        )
        self.sapling_to_sapling_height = self._mine_and_sync_build_nodes(node0, build_nodes)
        assert_equal(self.sapling_to_sapling_height, _GRPC_SAPLING_TO_SAPLING_HEIGHT)

        orchard_fee = conventional_fee(4)
        orchard_amount = Decimal('12.5') - orchard_fee
        print("grpc_comparison: building transparent -> Orchard transaction")
        self.t_to_orchard_txid = wait_and_assert_operationid_status(
            node0,
            node0.z_sendmany(
                self.taddr,
                [{"address": self.orchard_ua_aux, "amount": orchard_amount}],
                1,
                orchard_fee,
                'NoPrivacy',
            ),
        )
        self.t_to_orchard_height = self._mine_and_sync_build_nodes(node0, build_nodes)
        assert_equal(self.t_to_orchard_height, _GRPC_T_TO_ORCHARD_HEIGHT)
        # Restart the Orchard-owning wallet after funding lands so it reloads
        # its Orchard note state before the first Orchard spend.
        node1 = self._restart_build_node(build_nodes, 1)
        assert_equal(node1.getblockcount(), node0.getblockcount())

        print("grpc_comparison: building Orchard -> Orchard transaction")
        self.orchard_to_orchard_txid = wait_and_assert_operationid_status(
            node1,
            node1.z_sendmany(
                self.orchard_ua1,
                [{"address": self.orchard_addr1, "amount": amount}],
                1,
                ZIP_317_FEE,
            ),
        )
        _relay_raw_transaction(node1, node0, self.orchard_to_orchard_txid)
        self.orchard_to_orchard_height = self._mine_and_sync_build_nodes(node0, build_nodes)
        assert_equal(self.orchard_to_orchard_height, _GRPC_ORCHARD_TO_ORCHARD_HEIGHT)

        node1 = self._restart_build_node(build_nodes, 1)
        assert_equal(node1.getblockcount(), node0.getblockcount())

        print("grpc_comparison: building Orchard -> Sapling transaction")
        self.orchard_to_sapling_txid = wait_and_assert_operationid_status(
            node1,
            node1.z_sendmany(
                self.orchard_ua1,
                [{"address": self.sapling_addr1, "amount": amount}],
                1,
                ZIP_317_FEE,
                'AllowRevealedAmounts',
            ),
        )
        _relay_raw_transaction(node1, node0, self.orchard_to_sapling_txid)
        self.orchard_to_sapling_height = self._mine_and_sync_build_nodes(node0, build_nodes)
        assert_equal(self.orchard_to_sapling_height, _GRPC_ORCHARD_TO_SAPLING_HEIGHT)

        node1 = self._restart_build_node(build_nodes, 1)
        assert_equal(node1.getblockcount(), node0.getblockcount())

        print("grpc_comparison: building Sapling -> transparent transaction")
        self.sapling_to_t_txid = wait_and_assert_operationid_status(
            node0,
            node0.z_sendmany(
                self.sapling_ua_aux,
                [{"address": self.taddr, "amount": amount}],
                1,
                ZIP_317_FEE,
                'AllowRevealedRecipients',
            ),
        )
        self.sapling_to_t_height = self._mine_and_sync_build_nodes(node0, build_nodes)
        assert_equal(self.sapling_to_t_height, _GRPC_SAPLING_TO_T_HEIGHT)

        print("grpc_comparison: building Orchard -> transparent transaction")
        self.orchard_to_t_txid = wait_and_assert_operationid_status(
            node1,
            node1.z_sendmany(
                self.orchard_ua1,
                [{"address": self.taddr, "amount": amount}],
                1,
                ZIP_317_FEE,
                'AllowRevealedRecipients',
            ),
        )
        _relay_raw_transaction(node1, node0, self.orchard_to_t_txid)
        self.orchard_to_t_height = self._mine_and_sync_build_nodes(node0, build_nodes)
        assert_equal(self.orchard_to_t_height, _GRPC_ORCHARD_TO_T_HEIGHT)

    def setup_nodes(self):
        # Match Zebra regtest defaults up to Canopy, and activate Orchard-era
        # upgrades at the start of the shielded fixture range.
        args = [ZebraArgs(activation_heights={
            "NU5": _GRPC_ACTIVATION_HEIGHT,
            "NU6": _GRPC_ACTIVATION_HEIGHT,
        }, checkpoints=self._zebra_checkpoints) for _ in range(self.num_nodes)]
        return start_nodes(self.num_nodes, self.options.tmpdir,
                           args)

    def setup_network(self, split=False):
        self.wallets = []  # no wallets used; required for teardown
        self.nodes = []
        self.zcashd_nodes = []
        if self._chain_loaded_from_cache:
            print("grpc_comparison: restoring Zebrad chain from cache")
            self.nodes = self.setup_nodes()
            self.txid = self.nodes[0].getblock("1")['tx'][0]
        else:
            if self._stage1_loaded_from_cache:
                print("grpc_comparison: resuming from stage-1 wallet cache")
                build_nodes = self._start_build_nodes()
                self._wait_for_build_nodes_height(build_nodes, _GRPC_STAGE1_HEIGHT)
            else:
                print("grpc_comparison: building fresh stage-1 fixture chain with standalone zcashd")
                build_nodes = self._build_stage1_with_wallet_nodes()
                build_nodes = self._persist_stage1_cache(build_nodes)
            try:
                print("grpc_comparison: building stage-2 shielded transactions")
                self._complete_chain_from_stage1(build_nodes)
                # TODO: Re-home this fixture once standalone zcashd is retired.
                # Today we still rely on standalone zcashd to author the
                # shielded transactions, then replay the resulting chain into
                # Zebrad for the actual parity checks.
                #
                # Zebra and standalone zcashd disagree on regtest difficulty
                # throughout this standalone fixture, so replay via checkpoints
                # before starting the downstream indexers.
                checkpoint_path = os.path.join(self.options.tmpdir, 'grpc_comparison_checkpoints.txt')
                _write_checkpoint_file(build_nodes[0], build_nodes[0].getblockcount(), checkpoint_path)
                self._zebra_checkpoints = checkpoint_path
                print("grpc_comparison: starting Zebrad")
                self.nodes = self.setup_nodes()
                print("grpc_comparison: replaying built chain into Zebrad")
                _submit_missing_blocks(build_nodes[0], self.nodes[0])
                assert_equal(self.nodes[0].getblockcount(), build_nodes[0].getblockcount())
                print("grpc_comparison: waiting for Zebrad tip")
                self._wait_for_zebra_tip(build_nodes[0].getblockcount())
            finally:
                for index, node in enumerate(build_nodes):
                    stop_zcashd_node(index, node)
            print("grpc_comparison: persisting fresh Zebrad cache")
            self._persist_framework_cache()
            self.txid = self.nodes[0].getblock("1")['tx'][0]

        print("grpc_comparison: waiting for restored Zebrad tip before starting indexers")
        self._wait_for_zebra_tip(self.orchard_to_t_height)
        self.zainos = self.setup_indexers()
        self.lwds = self.setup_lightwalletds()

        # Wait for both indexers to sync to the chain tip before running tests.
        self._wait_for_indexers(self.nodes[0].getblockcount())

    def _wait_for_indexers(self, expected_height, timeout=60):
        """Block until both Zainod and Lightwalletd report the expected block height."""
        print("grpc_comparison: waiting for indexers to sync to height %d" % expected_height)
        zainod_ch = grpc.insecure_channel(f"127.0.0.1:{zaino_grpc_port(0)}")
        lwd_ch = grpc.insecure_channel(f"127.0.0.1:{self.lwds[0]}")
        try:
            zs = service_pb2_grpc.CompactTxStreamerStub(zainod_ch)
            ls = service_pb2_grpc.CompactTxStreamerStub(lwd_ch)

            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    z_info = zs.GetLightdInfo(service_pb2.Empty(), timeout=5)
                    l_info = ls.GetLightdInfo(service_pb2.Empty(), timeout=5)
                    if (z_info.blockHeight >= expected_height and
                            l_info.blockHeight >= expected_height):
                        return
                except grpc.RpcError:
                    pass
                time.sleep(1)

            raise Exception(
                f"Indexers did not sync to height {expected_height} within {timeout}s"
            )
        finally:
            zainod_ch.close()
            lwd_ch.close()

    def _wait_for_zebra_tip(self, expected_height, timeout=30):
        """Wait until Zebrad reports the expected tip height."""
        zebra = self.nodes[0]
        deadline = time.time() + timeout
        last_height = None
        while time.time() < deadline:
            last_height = zebra.getblockcount()
            if last_height >= expected_height:
                return
            time.sleep(1)

        raise AssertionError(
            "Zebrad did not reach height %d within %ds (last height %s)"
            % (expected_height, timeout, last_height)
        )

    def _run_checks(self, checks):
        """Run a sequence of labeled test helpers in order."""
        for label, method in checks:
            print("Testing %s..." % label)
            method()

    def run_test(self):
        zainod_ch = grpc.insecure_channel(f"127.0.0.1:{zaino_grpc_port(0)}")
        lwd_ch = grpc.insecure_channel(f"127.0.0.1:{self.lwds[0]}")
        zs = service_pb2_grpc.CompactTxStreamerStub(zainod_ch)
        ls = service_pb2_grpc.CompactTxStreamerStub(lwd_ch)

        # Start with chain-wide parity checks on transparent and metadata APIs.
        self._run_checks([
            ("GetLightdInfo", lambda: self.test_get_lightd_info(zs, ls)),
            ("GetLatestBlock", lambda: self.test_get_latest_block(zs, ls)),
            ("GetBlock", lambda: self.test_get_block(zs, ls)),
            ("GetBlock (out of bounds)", lambda: self.test_get_block_out_of_bounds(zs, ls)),
            ("GetBlockNullifiers", lambda: self.test_get_block_nullifiers(zs, ls)),
            ("GetBlockRange (forward)", lambda: self.test_get_block_range(zs, ls)),
            ("GetBlockRange (reverse)", lambda: self.test_get_block_range_reverse(zs, ls)),
            ("GetBlockRange (out of bounds)", lambda: self.test_get_block_range_out_of_bounds(zs, ls)),
            ("GetBlockRangeNullifiers", lambda: self.test_get_block_range_nullifiers(zs, ls)),
            ("GetBlockRangeNullifiers (reverse)", lambda: self.test_get_block_range_nullifiers_reverse(zs, ls)),
            ("GetTransaction", lambda: self.test_get_transaction(zs, ls)),
            ("GetTaddressTxids (full range)", lambda: self.test_get_taddress_txids(zs, ls)),
            ("GetTaddressTxids (tip-only range)", lambda: self.test_get_taddress_txids_tip_only(zs, ls)),
            ("GetTaddressTxids (genesis-only range)", lambda: self.test_get_taddress_txids_genesis_only(zs, ls)),
            ("GetTaddressBalance", lambda: self.test_get_taddress_balance(zs, ls)),
            ("GetTaddressBalanceStream", lambda: self.test_get_taddress_balance_stream(zs, ls)),
            ("GetTreeState (by height)", lambda: self.test_get_tree_state_by_height(zs, ls)),
            ("GetTreeState (out of bounds)", lambda: self.test_get_tree_state_out_of_bounds(zs, ls)),
            ("GetLatestTreeState", lambda: self.test_get_latest_tree_state(zs, ls)),
            ("GetSubtreeRoots (sapling)", lambda: self.test_get_subtree_roots_sapling(zs, ls)),
            ("GetSubtreeRoots (orchard)", lambda: self.test_get_subtree_roots_orchard(zs, ls)),
            ("GetAddressUtxos", lambda: self.test_get_address_utxos(zs, ls)),
            ("GetAddressUtxosStream", lambda: self.test_get_address_utxos_stream(zs, ls)),
        ])

        # Then walk the shielded fixture in chain order so each assertion lines
        # up with the block narrative at the top of the file.
        self._run_checks([
            ("GetBlock (t→Sapling, block %d)" % self.t_to_sapling_height,
             lambda: self.test_get_block_t_to_sapling(zs, ls)),
            ("GetBlockNullifiers (t→Sapling)",
             lambda: self.test_get_block_nullifiers_t_to_sapling(zs, ls)),
            ("GetBlockRange (shielded range %d–%d)" % (self.t_to_sapling_height, self.orchard_to_t_height),
             lambda: self.test_get_block_range_shielded(zs, ls)),
            ("GetTransaction (t→Sapling)",
             lambda: self.test_get_transaction_t_to_sapling(zs, ls)),
            ("GetTreeState (after t→Sapling, block %d)" % self.t_to_sapling_height,
             lambda: self.test_get_tree_state_after_t_to_sapling(zs, ls)),
            ("GetBlock (Sapling→Orchard, block %d)" % self.sapling_to_orchard_height,
             lambda: self.test_get_block_sapling_to_orchard(zs, ls)),
            ("GetTransaction (Sapling→Orchard)",
             lambda: self.test_get_transaction_sapling_to_orchard(zs, ls)),
            ("GetBlock (Sapling→Sapling, block %d)" % self.sapling_to_sapling_height,
             lambda: self.test_get_block_sapling_to_sapling(zs, ls)),
            ("GetTransaction (Sapling→Sapling)",
             lambda: self.test_get_transaction_sapling_to_sapling(zs, ls)),
            ("GetBlock (t→Orchard, block %d)" % self.t_to_orchard_height,
             lambda: self.test_get_block_t_to_orchard(zs, ls)),
            ("GetTransaction (t→Orchard)",
             lambda: self.test_get_transaction_t_to_orchard(zs, ls)),
            ("GetTreeState (after t→Orchard, block %d)" % self.t_to_orchard_height,
             lambda: self.test_get_tree_state_after_t_to_orchard(zs, ls)),
            ("GetBlock (Orchard→Orchard, block %d)" % self.orchard_to_orchard_height,
             lambda: self.test_get_block_orchard_to_orchard(zs, ls)),
            ("GetTransaction (Orchard→Orchard)",
             lambda: self.test_get_transaction_orchard_to_orchard(zs, ls)),
            ("GetBlock (Orchard→Sapling, block %d)" % self.orchard_to_sapling_height,
             lambda: self.test_get_block_orchard_to_sapling(zs, ls)),
            ("GetTransaction (Orchard→Sapling)",
             lambda: self.test_get_transaction_orchard_to_sapling(zs, ls)),
            ("GetBlock (Sapling→t, block %d)" % self.sapling_to_t_height,
             lambda: self.test_get_block_sapling_to_t(zs, ls)),
            ("GetTransaction (Sapling→t)",
             lambda: self.test_get_transaction_sapling_to_t(zs, ls)),
            ("GetBlock (Orchard→t, block %d)" % self.orchard_to_t_height,
             lambda: self.test_get_block_orchard_to_t(zs, ls)),
            ("GetTransaction (Orchard→t)",
             lambda: self.test_get_transaction_orchard_to_t(zs, ls)),
        ])

        # TODO: GetMempoolTx and GetMempoolStream require submitting a transaction
        # to the mempool via the mempool RPC.

        zainod_ch.close()
        lwd_ch.close()

    # -------------------------------------------------------------------------
    # Test methods
    # -------------------------------------------------------------------------

    def test_get_lightd_info(self, zs, ls):
        z = zs.GetLightdInfo(service_pb2.Empty())
        l = ls.GetLightdInfo(service_pb2.Empty())

        # Implementation-specific fields are intentionally skipped:
        #   version, vendor, git_commit, branch, build_date, build_user,
        #   zcashd_build, zcashd_subversion, donation_address,
        #   lightwallet_protocol_version
        assert_equal(z.taddrSupport, l.taddrSupport)
        assert_equal(z.chainName, l.chainName)
        assert_equal(z.saplingActivationHeight, l.saplingActivationHeight)
        assert_equal(z.consensusBranchId, l.consensusBranchId)
        assert_equal(z.blockHeight, l.blockHeight)
        assert_equal(z.estimatedHeight, l.estimatedHeight)

    def test_get_latest_block(self, zs, ls):
        z = zs.GetLatestBlock(service_pb2.ChainSpec())
        l = ls.GetLatestBlock(service_pb2.ChainSpec())
        assert_equal(z.height, l.height)
        assert_equal(z.hash, l.hash)

    def test_get_block(self, zs, ls):
        req = service_pb2.BlockID(height=5, hash=b"")
        z = _strict_compact_block(zs.GetBlock(req))
        l = _strict_compact_block(ls.GetBlock(req))
        _assert_compact_block_equal("GetBlock", z, l)

    def test_get_block_out_of_bounds(self, zs, ls):
        # Height beyond chain tip — both must respond with a gRPC error.
        # Note: Zainod returns OUT_OF_RANGE; Lightwalletd returns INVALID_ARGUMENT.
        # We only assert that both raise an error, not that the codes match.
        chain_height = self.nodes[0].getblockcount()
        req = service_pb2.BlockID(height=chain_height + 1000, hash=b"")
        try:
            zs.GetBlock(req)
            raise AssertionError("Zainod did not error on out-of-range GetBlock")
        except grpc.RpcError:
            pass
        try:
            ls.GetBlock(req)
            raise AssertionError("Lightwalletd did not error on out-of-range GetBlock")
        except grpc.RpcError:
            pass

    def test_get_block_nullifiers(self, zs, ls):
        req = service_pb2.BlockID(height=5, hash=b"")
        z = _strict_compact_block(zs.GetBlockNullifiers(req))
        l = _strict_compact_block(ls.GetBlockNullifiers(req))
        _assert_compact_block_equal("GetBlockNullifiers", z, l)

    def test_get_block_range(self, zs, ls):
        req = service_pb2.BlockRange(
            start=service_pb2.BlockID(height=1, hash=b""),
            end=service_pb2.BlockID(height=10, hash=b""),
        )
        z_blocks = [_strict_compact_block(b) for b in _collect_stream(zs.GetBlockRange(req))]
        l_blocks = [_strict_compact_block(b) for b in _collect_stream(ls.GetBlockRange(req))]
        assert_equal(len(z_blocks), len(l_blocks))
        for z_b, l_b in zip(z_blocks, l_blocks):
            _assert_compact_block_equal("GetBlockRange", z_b, l_b)

    def test_get_block_range_reverse(self, zs, ls):
        req = service_pb2.BlockRange(
            start=service_pb2.BlockID(height=10, hash=b""),
            end=service_pb2.BlockID(height=1, hash=b""),
        )
        z_blocks = [_strict_compact_block(b) for b in _collect_stream(zs.GetBlockRange(req))]
        l_blocks = [_strict_compact_block(b) for b in _collect_stream(ls.GetBlockRange(req))]
        assert_equal(len(z_blocks), len(l_blocks))
        for z_b, l_b in zip(z_blocks, l_blocks):
            _assert_compact_block_equal("GetBlockRange reverse", z_b, l_b)

    def test_get_block_range_out_of_bounds(self, zs, ls):
        # Both must respond with a gRPC error when the range exceeds the chain tip.
        # Note: implementations may return different status codes (OUT_OF_RANGE vs
        # INVALID_ARGUMENT), so we only assert that both raise an error.
        chain_height = self.nodes[0].getblockcount()
        req = service_pb2.BlockRange(
            start=service_pb2.BlockID(height=1, hash=b""),
            end=service_pb2.BlockID(height=chain_height + 1000, hash=b""),
        )
        try:
            list(zs.GetBlockRange(req))
            raise AssertionError("Zainod did not error on out-of-range GetBlockRange")
        except grpc.RpcError:
            pass
        try:
            list(ls.GetBlockRange(req))
            raise AssertionError("Lightwalletd did not error on out-of-range GetBlockRange")
        except grpc.RpcError:
            pass

    def test_get_block_range_nullifiers(self, zs, ls):
        req = service_pb2.BlockRange(
            start=service_pb2.BlockID(height=1, hash=b""),
            end=service_pb2.BlockID(height=10, hash=b""),
        )
        z_blocks = [_strict_compact_block(b) for b in _collect_stream(zs.GetBlockRangeNullifiers(req))]
        l_blocks = [_strict_compact_block(b) for b in _collect_stream(ls.GetBlockRangeNullifiers(req))]
        assert_equal(len(z_blocks), len(l_blocks))
        for z_b, l_b in zip(z_blocks, l_blocks):
            _assert_compact_block_equal("GetBlockRangeNullifiers", z_b, l_b)

    def test_get_block_range_nullifiers_reverse(self, zs, ls):
        req = service_pb2.BlockRange(
            start=service_pb2.BlockID(height=10, hash=b""),
            end=service_pb2.BlockID(height=1, hash=b""),
        )
        z_blocks = [_strict_compact_block(b) for b in _collect_stream(zs.GetBlockRangeNullifiers(req))]
        l_blocks = [_strict_compact_block(b) for b in _collect_stream(ls.GetBlockRangeNullifiers(req))]
        assert_equal(len(z_blocks), len(l_blocks))
        for z_b, l_b in zip(z_blocks, l_blocks):
            _assert_compact_block_equal("GetBlockRangeNullifiers reverse", z_b, l_b)

    def test_get_transaction(self, zs, ls):
        # self.txid is a hex string; the TxFilter expects bytes in little-endian order
        txid_bytes = bytes.fromhex(self.txid)[::-1]
        req = service_pb2.TxFilter(hash=txid_bytes)
        z = zs.GetTransaction(req)
        l = ls.GetTransaction(req)
        assert_equal(z.data, l.data)
        assert_equal(z.height, l.height)

    def test_get_taddress_txids(self, zs, ls):
        req = service_pb2.TransparentAddressBlockFilter(
            address=self.taddr,
            range=service_pb2.BlockRange(
                start=service_pb2.BlockID(height=1, hash=b""),
                end=service_pb2.BlockID(height=self.nodes[0].getblockcount(), hash=b""),
            ),
        )
        z_txs = _collect_stream(zs.GetTaddressTxids(req))
        l_txs = _collect_stream(ls.GetTaddressTxids(req))
        assert_equal(len(z_txs), len(l_txs))
        for z_tx, l_tx in zip(z_txs, l_txs):
            assert_equal(z_tx.data, l_tx.data)
            assert_equal(z_tx.height, l_tx.height)

    def test_get_taddress_txids_tip_only(self, zs, ls):
        tip = self.nodes[0].getblockcount()
        req = service_pb2.TransparentAddressBlockFilter(
            address=self.taddr,
            range=service_pb2.BlockRange(
                start=service_pb2.BlockID(height=tip, hash=b""),
                end=service_pb2.BlockID(height=tip, hash=b""),
            ),
        )
        z_txs = _collect_stream(zs.GetTaddressTxids(req))
        l_txs = _collect_stream(ls.GetTaddressTxids(req))
        assert_equal(len(z_txs), len(l_txs))
        for z_tx, l_tx in zip(z_txs, l_txs):
            assert_equal(z_tx.data, l_tx.data)
            assert_equal(z_tx.height, l_tx.height)

    def test_get_taddress_txids_genesis_only(self, zs, ls):
        req = service_pb2.TransparentAddressBlockFilter(
            address=self.taddr,
            range=service_pb2.BlockRange(
                start=service_pb2.BlockID(height=1, hash=b""),
                end=service_pb2.BlockID(height=1, hash=b""),
            ),
        )
        z_txs = _collect_stream(zs.GetTaddressTxids(req))
        l_txs = _collect_stream(ls.GetTaddressTxids(req))
        assert_equal(len(z_txs), len(l_txs))
        for z_tx, l_tx in zip(z_txs, l_txs):
            assert_equal(z_tx.data, l_tx.data)
            assert_equal(z_tx.height, l_tx.height)

    def test_get_taddress_balance(self, zs, ls):
        req = service_pb2.AddressList(addresses=[self.taddr])
        z = zs.GetTaddressBalance(req)
        l = ls.GetTaddressBalance(req)
        assert_equal(z.valueZat, l.valueZat)

    def test_get_taddress_balance_stream(self, zs, ls):
        def addr_iter():
            yield service_pb2.Address(address=self.taddr)

        z = zs.GetTaddressBalanceStream(addr_iter())
        l = ls.GetTaddressBalanceStream(addr_iter())
        assert_equal(z.valueZat, l.valueZat)

    def test_get_tree_state_by_height(self, zs, ls):
        req = service_pb2.BlockID(height=10, hash=b"")
        z = zs.GetTreeState(req)
        l = ls.GetTreeState(req)
        assert_equal(z.network, l.network)
        assert_equal(z.height, l.height)
        assert_equal(z.hash, l.hash)
        assert_equal(z.time, l.time)
        assert_equal(z.saplingTree, l.saplingTree)
        assert_equal(z.orchardTree, l.orchardTree)

    def test_get_tree_state_out_of_bounds(self, zs, ls):
        # Both must respond with a gRPC error for an out-of-range height.
        # Note: Zainod returns OUT_OF_RANGE; Lightwalletd returns INVALID_ARGUMENT.
        chain_height = self.nodes[0].getblockcount()
        req = service_pb2.BlockID(height=chain_height + 1000, hash=b"")
        try:
            zs.GetTreeState(req)
            raise AssertionError("Zainod did not error on out-of-range GetTreeState")
        except grpc.RpcError:
            pass
        try:
            ls.GetTreeState(req)
            raise AssertionError("Lightwalletd did not error on out-of-range GetTreeState")
        except grpc.RpcError:
            pass

    def test_get_latest_tree_state(self, zs, ls):
        z = zs.GetLatestTreeState(service_pb2.Empty())
        l = ls.GetLatestTreeState(service_pb2.Empty())
        assert_equal(z.network, l.network)
        assert_equal(z.height, l.height)
        assert_equal(z.hash, l.hash)
        assert_equal(z.saplingTree, l.saplingTree)
        assert_equal(z.orchardTree, l.orchardTree)

    def test_get_subtree_roots_sapling(self, zs, ls):
        req = service_pb2.GetSubtreeRootsArg(
            startIndex=0,
            shieldedProtocol=service_pb2.ShieldedProtocol.sapling,
            maxEntries=0,
        )
        z_roots = _collect_stream(zs.GetSubtreeRoots(req))
        l_roots = _collect_stream(ls.GetSubtreeRoots(req))
        assert_equal(len(z_roots), len(l_roots))
        for z_r, l_r in zip(z_roots, l_roots):
            assert_equal(z_r.rootHash, l_r.rootHash)
            assert_equal(z_r.completingBlockHash, l_r.completingBlockHash)
            assert_equal(z_r.completingBlockHeight, l_r.completingBlockHeight)

    def test_get_subtree_roots_orchard(self, zs, ls):
        req = service_pb2.GetSubtreeRootsArg(
            startIndex=0,
            shieldedProtocol=service_pb2.ShieldedProtocol.orchard,
            maxEntries=0,
        )
        z_roots = _collect_stream(zs.GetSubtreeRoots(req))
        l_roots = _collect_stream(ls.GetSubtreeRoots(req))
        assert_equal(len(z_roots), len(l_roots))
        for z_r, l_r in zip(z_roots, l_roots):
            assert_equal(z_r.rootHash, l_r.rootHash)
            assert_equal(z_r.completingBlockHash, l_r.completingBlockHash)
            assert_equal(z_r.completingBlockHeight, l_r.completingBlockHeight)

    def test_get_address_utxos(self, zs, ls):
        req = service_pb2.GetAddressUtxosArg(
            addresses=[self.taddr],
            startHeight=1,
            maxEntries=0,
        )
        z = zs.GetAddressUtxos(req)
        l = ls.GetAddressUtxos(req)
        assert_equal(len(z.addressUtxos), len(l.addressUtxos))
        z_sorted = sorted(z.addressUtxos, key=lambda u: (u.txid, u.index))
        l_sorted = sorted(l.addressUtxos, key=lambda u: (u.txid, u.index))
        for z_u, l_u in zip(z_sorted, l_sorted):
            assert_equal(z_u.address, l_u.address)
            assert_equal(z_u.txid, l_u.txid)
            assert_equal(z_u.index, l_u.index)
            assert_equal(z_u.script, l_u.script)
            assert_equal(z_u.valueZat, l_u.valueZat)
            assert_equal(z_u.height, l_u.height)

    def test_get_address_utxos_stream(self, zs, ls):
        req = service_pb2.GetAddressUtxosArg(
            addresses=[self.taddr],
            startHeight=1,
            maxEntries=0,
        )
        z_utxos = _collect_stream(zs.GetAddressUtxosStream(req))
        l_utxos = _collect_stream(ls.GetAddressUtxosStream(req))
        assert_equal(len(z_utxos), len(l_utxos))
        z_sorted = sorted(z_utxos, key=lambda u: (u.txid, u.index))
        l_sorted = sorted(l_utxos, key=lambda u: (u.txid, u.index))
        for z_u, l_u in zip(z_sorted, l_sorted):
            assert_equal(z_u.address, l_u.address)
            assert_equal(z_u.txid, l_u.txid)
            assert_equal(z_u.index, l_u.index)
            assert_equal(z_u.script, l_u.script)
            assert_equal(z_u.valueZat, l_u.valueZat)
            assert_equal(z_u.height, l_u.height)

    # -------------------------------------------------------------------------
    # Shielded transaction tests (the shielded fixture range)
    #
    # Every block in the shielded range has at least one shielded component
    # (Sapling spend/output or Orchard action), so vtx must be non-empty and
    # identical across both Zainod and Lightwalletd.
    # -------------------------------------------------------------------------

    def _assert_shielded_block_match(self, zs, ls, height, label):
        req = service_pb2.BlockID(height=height, hash=b"")
        z = _strict_compact_block(zs.GetBlock(req))
        l = _strict_compact_block(ls.GetBlock(req))
        assert_true(len(z.vtx) > 0,
                    "Zainod returned empty vtx for %s block at height %d" % (label, height))
        assert_true(len(l.vtx) > 0,
                    "Lightwalletd returned empty vtx for %s block at height %d" % (label, height))
        _assert_compact_block_equal("GetBlock %s" % label, z, l)

    def _assert_transaction_match(self, zs, ls, txid_hex, expected_height):
        txid_bytes = bytes.fromhex(txid_hex)[::-1]
        req = service_pb2.TxFilter(hash=txid_bytes)
        z = zs.GetTransaction(req)
        l = ls.GetTransaction(req)
        assert_equal(z.data, l.data)
        assert_equal(z.height, l.height)
        assert_equal(z.height, expected_height)

    # -- t → Sapling (block 201) --

    def test_get_block_t_to_sapling(self, zs, ls):
        """Block with a t→Sapling output must have matching, non-empty vtx."""
        self._assert_shielded_block_match(zs, ls, self.t_to_sapling_height, 't→Sapling')

    def test_get_block_nullifiers_t_to_sapling(self, zs, ls):
        req = service_pb2.BlockID(height=self.t_to_sapling_height, hash=b"")
        z = _strict_compact_block(zs.GetBlockNullifiers(req))
        l = _strict_compact_block(ls.GetBlockNullifiers(req))
        _assert_compact_block_equal("GetBlockNullifiers t→Sapling", z, l)

    def test_get_block_range_shielded(self, zs, ls):
        """
        All blocks in the shielded range must have matching, non-empty vtx.

        This range check intentionally keeps the streamed CompactTx entries
        intact so it can detect GetBlockRange divergences between Zainod and
        Lightwalletd.
        """
        start = self.t_to_sapling_height
        end = self.orchard_to_t_height
        req = service_pb2.BlockRange(
            start=service_pb2.BlockID(height=start, hash=b""),
            end=service_pb2.BlockID(height=end, hash=b""),
        )
        z_blocks = [_strict_compact_block(b) for b in _collect_stream(zs.GetBlockRange(req))]
        l_blocks = [_strict_compact_block(b) for b in _collect_stream(ls.GetBlockRange(req))]
        assert_equal(len(z_blocks), len(l_blocks))
        for z_b, l_b in zip(z_blocks, l_blocks):
            assert_true(len(z_b.vtx) > 0,
                        "Zainod returned empty vtx for shielded block at height %d" % z_b.height)
            assert_true(len(l_b.vtx) > 0,
                        "Lightwalletd returned empty vtx for shielded block at height %d" % l_b.height)
            _assert_compact_block_equal("GetBlockRange shielded", z_b, l_b)

    def test_get_transaction_t_to_sapling(self, zs, ls):
        """t→Sapling transaction bytes and height must match across both indexers."""
        self._assert_transaction_match(
            zs, ls, self.t_to_sapling_txid, self.t_to_sapling_height)

    def test_get_tree_state_after_t_to_sapling(self, zs, ls):
        """After t→Sapling (block 201) Sapling must be non-empty.
        Orchard tree state is already initialized on this chain layout, so we
        only assert parity and Sapling population here."""
        req = service_pb2.BlockID(height=self.t_to_sapling_height, hash=b"")
        z = zs.GetTreeState(req)
        l = ls.GetTreeState(req)
        assert_equal(z.network, l.network)
        assert_equal(z.height, l.height)
        assert_equal(z.hash, l.hash)
        assert_equal(z.saplingTree, l.saplingTree)
        assert_equal(z.orchardTree, l.orchardTree)
        assert_true(len(z.saplingTree) > 0,
                    "Sapling tree is empty after t→Sapling tx at height %d" % self.t_to_sapling_height)

    # -- t → Orchard (block 205) --

    def test_get_block_t_to_orchard(self, zs, ls):
        """Block with a t→Orchard output must have matching, non-empty vtx."""
        self._assert_shielded_block_match(zs, ls, self.t_to_orchard_height, 't→Orchard')

    def test_get_transaction_t_to_orchard(self, zs, ls):
        """t→Orchard transaction bytes and height must match across both indexers."""
        self._assert_transaction_match(
            zs, ls, self.t_to_orchard_txid, self.t_to_orchard_height)

    def test_get_tree_state_after_t_to_orchard(self, zs, ls):
        """After t→Orchard (block 205) the Orchard tree must be non-empty."""
        req = service_pb2.BlockID(height=self.t_to_orchard_height, hash=b"")
        z = zs.GetTreeState(req)
        l = ls.GetTreeState(req)
        assert_equal(z.network, l.network)
        assert_equal(z.height, l.height)
        assert_equal(z.hash, l.hash)
        assert_equal(z.saplingTree, l.saplingTree)
        assert_equal(z.orchardTree, l.orchardTree)
        assert_true(len(z.orchardTree) > 0,
                    "Orchard tree is empty after t→Orchard coinbase at height %d" % self.t_to_orchard_height)

    # -- Sapling → Sapling (block 204) --

    def test_get_block_sapling_to_sapling(self, zs, ls):
        """Block with a Sapling→Sapling spend must have matching, non-empty vtx."""
        self._assert_shielded_block_match(
            zs, ls, self.sapling_to_sapling_height, 'Sapling→Sapling')

    def test_get_transaction_sapling_to_sapling(self, zs, ls):
        """Sapling→Sapling transaction bytes and height must match across both indexers."""
        self._assert_transaction_match(
            zs, ls, self.sapling_to_sapling_txid, self.sapling_to_sapling_height)

    # -- Orchard → Orchard (block 206) --

    def test_get_block_orchard_to_orchard(self, zs, ls):
        """Block with an Orchard→Orchard spend must have matching, non-empty vtx."""
        self._assert_shielded_block_match(
            zs, ls, self.orchard_to_orchard_height, 'Orchard→Orchard')

    def test_get_transaction_orchard_to_orchard(self, zs, ls):
        """Orchard→Orchard transaction bytes and height must match across both indexers."""
        self._assert_transaction_match(
            zs, ls, self.orchard_to_orchard_txid, self.orchard_to_orchard_height)

    # -- Sapling → Orchard (block 203) --

    def test_get_block_sapling_to_orchard(self, zs, ls):
        """Block with a Sapling→Orchard (cross-pool) tx must have matching, non-empty vtx."""
        self._assert_shielded_block_match(
            zs, ls, self.sapling_to_orchard_height, 'Sapling→Orchard')

    def test_get_transaction_sapling_to_orchard(self, zs, ls):
        """Sapling→Orchard transaction bytes and height must match across both indexers."""
        self._assert_transaction_match(
            zs, ls, self.sapling_to_orchard_txid, self.sapling_to_orchard_height)

    # -- Orchard → Sapling (block 207) --

    def test_get_block_orchard_to_sapling(self, zs, ls):
        """Block with an Orchard→Sapling (cross-pool) tx must have matching, non-empty vtx."""
        self._assert_shielded_block_match(
            zs, ls, self.orchard_to_sapling_height, 'Orchard→Sapling')

    def test_get_transaction_orchard_to_sapling(self, zs, ls):
        """Orchard→Sapling transaction bytes and height must match across both indexers."""
        self._assert_transaction_match(
            zs, ls, self.orchard_to_sapling_txid, self.orchard_to_sapling_height)

    # -- Sapling → t (block 208) --

    def test_get_block_sapling_to_t(self, zs, ls):
        """Block with a Sapling→t tx must have matching, non-empty vtx (Sapling spend present)."""
        self._assert_shielded_block_match(
            zs, ls, self.sapling_to_t_height, 'Sapling→t')

    def test_get_transaction_sapling_to_t(self, zs, ls):
        """Sapling→t transaction bytes and height must match across both indexers."""
        self._assert_transaction_match(
            zs, ls, self.sapling_to_t_txid, self.sapling_to_t_height)

    # -- Orchard → t (block 209) --

    def test_get_block_orchard_to_t(self, zs, ls):
        """Block with an Orchard→t tx must have matching, non-empty vtx (Orchard action present)."""
        self._assert_shielded_block_match(
            zs, ls, self.orchard_to_t_height, 'Orchard→t')

    def test_get_transaction_orchard_to_t(self, zs, ls):
        """Orchard→t transaction bytes and height must match across both indexers."""
        self._assert_transaction_match(
            zs, ls, self.orchard_to_t_txid, self.orchard_to_t_height)


if __name__ == '__main__':
    GrpcComparisonTest().main()
