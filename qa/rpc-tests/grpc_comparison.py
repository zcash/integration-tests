#!/usr/bin/env python3
# Copyright (c) 2025 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

"""
gRPC parity test: compare CompactTxStreamer responses from Zainod and Lightwalletd
backed by the same Zebrad node.

Mirrors the Rust test fixtures in client_rpc_test_fixtures, porting them to Python
so they run inside the existing BitcoinTestFramework CI pipeline.

Methods tested (CompactTxStreamer service):
  GetLightdInfo, GetLatestBlock, GetBlock, GetBlockNullifiers,
  GetBlockRange, GetBlockRangeNullifiers,
  GetTransaction, GetTaddressTxids, GetTaddressBalance, GetTaddressBalanceStream,
  GetTreeState, GetLatestTreeState, GetSubtreeRoots,
  GetAddressUtxos, GetAddressUtxosStream

Not yet tested (require a wallet to submit mempool transactions):
  GetMempoolTx, GetMempoolStream
"""

import time

import grpc

from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import (
    assert_equal,
    assert_true,
    p2p_port,
    start_zcashd_node,
    sync_blocks,
    zaino_grpc_port,
    lwd_grpc_port,
)
from test_framework.proto import (
    compact_formats_pb2,
    service_pb2,
    service_pb2_grpc,
)


def _collect_stream(streaming_call):
    """Collect all messages from a server-streaming gRPC call into a list."""
    results = []
    for msg in streaming_call:
        results.append(msg)
    return results


def _normalize_compact_block(block):
    """
    Normalize a CompactBlock for header-level comparison.

    Known divergences between Zainod and Lightwalletd (with Zebrad as backend):
    - protoVersion: Lightwalletd sets 1, Zainod sets 4. Zeroed out.
    - vtx: Lightwalletd includes transparent coinbase transactions in compact
      blocks; Zainod omits them (only shielded transactions appear). Cleared.

    The header fields (height, hash, prevHash, time) are authoritative and
    must agree between both implementations.
    """
    normalized = compact_formats_pb2.CompactBlock()
    normalized.height = block.height
    normalized.hash = block.hash
    normalized.prevHash = block.prevHash
    normalized.time = block.time
    # chainMetadata is also comparable; copy it if present
    if block.HasField("chainMetadata"):
        normalized.chainMetadata.CopyFrom(block.chainMetadata)
    return normalized


def _normalize_shielded_compact_block(block):
    """
    Normalize a CompactBlock that contains shielded transactions for full
    comparison including vtx.

    For blocks whose coinbase is shielded (Sapling/Orchard), both Zainod and
    Lightwalletd must include the compact shielded outputs in vtx.  The only
    implementation-specific field zeroed here is protoVersion.
    """
    normalized = compact_formats_pb2.CompactBlock()
    normalized.CopyFrom(block)
    normalized.protoVersion = 0
    return normalized


class GrpcComparisonTest(BitcoinTestFramework):

    def __init__(self):
        super().__init__()
        self.num_nodes = 1
        self.num_indexers = 1        # Zainod
        self.num_lightwalletds = 1   # Lightwalletd
        self.num_wallets = 0
        self.cache_behavior = 'clean'

        # Populated in setup_network; used by test methods
        self.taddr = None
        self.txid = None
        self.sapling_addr = None
        self.shielded_coinbase_txid = None
        self.shielded_block_height = None
        self.orchard_addr = None
        self.orchard_coinbase_txid = None
        self.orchard_block_height = None

    def setup_network(self, split=False):
        self.wallets = []  # no wallets used; required for teardown

        # Start Zebrad (passive peer — does not mine).
        self.nodes = self.setup_nodes()
        zebrad = self.nodes[0]

        # Start zcashd peered with zebrad.  zcashd carries the built-in wallet
        # needed to generate addresses and mine shielded coinbase blocks.
        zcashd = start_zcashd_node(0, self.options.tmpdir, p2p_port(0))
        self.zcashd_nodes = [zcashd]

        # Wait for the zcashd → zebrad P2P connection to be established before
        # mining so that every block propagates immediately.
        zebrad_p2p = "127.0.0.1:%d" % p2p_port(0)
        deadline = time.time() + 30
        while time.time() < deadline:
            if any(p['addr'] == zebrad_p2p for p in zcashd.getpeerinfo()):
                break
            time.sleep(0.5)
        else:
            raise Exception("zcashd did not connect to zebrad within 30s")

        # Phase 1 — transparent coinbase (blocks 1-12).
        # Provides real t-addr data for GetTaddressTxids / GetTaddressBalance /
        # GetAddressUtxos tests.
        self.taddr = zcashd.getnewaddress()
        zcashd.setmineraddress(self.taddr)
        zcashd.generate(12)
        self.txid = zcashd.getblock("1")['tx'][0]

        # Generate a single unified address and extract its individual receivers
        # via z_listunifiedreceivers so we can mine explicitly to each pool.
        ua = zcashd.z_getnewaddress('unified')
        receivers = zcashd.z_listunifiedreceivers(ua)

        # Phase 2 — shielded coinbase to Sapling via ZIP 213 (blocks 13-24).
        # Heartwood is active from block 1, so a Sapling address is valid for mining.
        self.sapling_addr = receivers['sapling']
        zcashd.setmineraddress(self.sapling_addr)
        shielded_start = zcashd.getblockcount() + 1  # = 13
        zcashd.generate(12)
        self.shielded_block_height = shielded_start
        self.shielded_coinbase_txid = zcashd.getblock(str(shielded_start))['tx'][0]

        # Phase 3 — shielded coinbase to Orchard via ZIP 213 + NU5 (blocks 25-36).
        # NU5 is active from block 1, so the Orchard receiver of the UA is valid
        # for mining.  This populates the Orchard commitment tree, allowing
        # GetTreeState to return a non-empty orchardTree.
        self.orchard_addr = receivers['orchard']
        zcashd.setmineraddress(self.orchard_addr)
        orchard_start = zcashd.getblockcount() + 1  # = 25
        zcashd.generate(12)
        self.orchard_block_height = orchard_start
        self.orchard_coinbase_txid = zcashd.getblock(str(orchard_start))['tx'][0]

        # Pad the chain to at least 100 blocks. Zainod requires a minimum of
        # 100 blocks to start (see test_framework.py:prepare_chain).
        # The three mining phases above produce 36 blocks; mine the remainder
        # as transparent coinbase so the shielded block heights stay stable.
        current_height = zcashd.getblockcount()
        if current_height < 100:
            zcashd.setmineraddress(self.taddr)
            zcashd.generate(100 - current_height)

        # Sync zebrad to zcashd's chain tip before starting the indexers.
        sync_blocks([zebrad, zcashd], timeout=120)

        self.zainos = self.setup_indexers()
        self.lwds = self.setup_lightwalletds()

        # Wait for both indexers to sync to the chain tip before running tests.
        tip = zebrad.getblockcount()
        self._wait_for_indexers(tip)

    def _wait_for_indexers(self, expected_height, timeout=60):
        """Block until both Zainod and Lightwalletd report the expected block height."""
        import time

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

    def run_test(self):
        zainod_ch = grpc.insecure_channel(f"127.0.0.1:{zaino_grpc_port(0)}")
        lwd_ch = grpc.insecure_channel(f"127.0.0.1:{self.lwds[0]}")
        zs = service_pb2_grpc.CompactTxStreamerStub(zainod_ch)
        ls = service_pb2_grpc.CompactTxStreamerStub(lwd_ch)

        print("Testing GetLightdInfo...")
        self.test_get_lightd_info(zs, ls)

        print("Testing GetLatestBlock...")
        self.test_get_latest_block(zs, ls)

        print("Testing GetBlock...")
        self.test_get_block(zs, ls)

        print("Testing GetBlock (out of bounds)...")
        self.test_get_block_out_of_bounds(zs, ls)

        print("Testing GetBlockNullifiers...")
        self.test_get_block_nullifiers(zs, ls)

        print("Testing GetBlockRange (forward)...")
        self.test_get_block_range(zs, ls)

        print("Testing GetBlockRange (reverse)...")
        self.test_get_block_range_reverse(zs, ls)

        print("Testing GetBlockRange (out of bounds)...")
        self.test_get_block_range_out_of_bounds(zs, ls)

        print("Testing GetBlockRangeNullifiers...")
        self.test_get_block_range_nullifiers(zs, ls)

        print("Testing GetBlockRangeNullifiers (reverse)...")
        self.test_get_block_range_nullifiers_reverse(zs, ls)

        print("Testing GetTransaction...")
        self.test_get_transaction(zs, ls)

        print("Testing GetTaddressTxids (full range)...")
        self.test_get_taddress_txids(zs, ls)

        print("Testing GetTaddressTxids (lower bound)...")
        self.test_get_taddress_txids_lower(zs, ls)

        print("Testing GetTaddressTxids (upper bound)...")
        self.test_get_taddress_txids_upper(zs, ls)

        print("Testing GetTaddressBalance...")
        self.test_get_taddress_balance(zs, ls)

        print("Testing GetTaddressBalanceStream...")
        self.test_get_taddress_balance_stream(zs, ls)

        print("Testing GetTreeState (by height)...")
        self.test_get_tree_state_by_height(zs, ls)

        print("Testing GetTreeState (out of bounds)...")
        self.test_get_tree_state_out_of_bounds(zs, ls)

        print("Testing GetLatestTreeState...")
        self.test_get_latest_tree_state(zs, ls)

        print("Testing GetSubtreeRoots (sapling)...")
        self.test_get_subtree_roots_sapling(zs, ls)

        print("Testing GetSubtreeRoots (orchard)...")
        self.test_get_subtree_roots_orchard(zs, ls)

        print("Testing GetAddressUtxos...")
        self.test_get_address_utxos(zs, ls)

        print("Testing GetAddressUtxosStream...")
        self.test_get_address_utxos_stream(zs, ls)

        # Shielded coinbase tests — blocks mined to a Sapling address via ZIP 213.
        print("Testing GetBlock (shielded coinbase)...")
        self.test_get_block_shielded(zs, ls)

        print("Testing GetBlockNullifiers (shielded coinbase)...")
        self.test_get_block_nullifiers_shielded(zs, ls)

        print("Testing GetBlockRange (shielded)...")
        self.test_get_block_range_shielded(zs, ls)

        print("Testing GetTransaction (shielded coinbase)...")
        self.test_get_transaction_shielded(zs, ls)

        print("Testing GetTreeState (after Sapling coinbase)...")
        self.test_get_tree_state_sapling(zs, ls)

        print("Testing GetBlock (Orchard coinbase)...")
        self.test_get_block_orchard(zs, ls)

        print("Testing GetTransaction (Orchard coinbase)...")
        self.test_get_transaction_orchard(zs, ls)

        print("Testing GetTreeState (after Orchard coinbase)...")
        self.test_get_tree_state_orchard(zs, ls)

        # TODO: GetMempoolTx and GetMempoolStream require submitting a transaction
        # to the mempool. This needs a wallet (zallet) to sign and send a raw tx.
        # Add these when num_wallets=1 is wired up with a t-address workflow.

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
        z = _normalize_compact_block(zs.GetBlock(req))
        l = _normalize_compact_block(ls.GetBlock(req))
        assert_equal(z, l)

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
        z = _normalize_compact_block(zs.GetBlockNullifiers(req))
        l = _normalize_compact_block(ls.GetBlockNullifiers(req))
        assert_equal(z, l)

    def test_get_block_range(self, zs, ls):
        req = service_pb2.BlockRange(
            start=service_pb2.BlockID(height=1, hash=b""),
            end=service_pb2.BlockID(height=10, hash=b""),
        )
        z_blocks = [_normalize_compact_block(b) for b in _collect_stream(zs.GetBlockRange(req))]
        l_blocks = [_normalize_compact_block(b) for b in _collect_stream(ls.GetBlockRange(req))]
        assert_equal(z_blocks, l_blocks)

    def test_get_block_range_reverse(self, zs, ls):
        req = service_pb2.BlockRange(
            start=service_pb2.BlockID(height=10, hash=b""),
            end=service_pb2.BlockID(height=1, hash=b""),
        )
        z_blocks = [_normalize_compact_block(b) for b in _collect_stream(zs.GetBlockRange(req))]
        l_blocks = [_normalize_compact_block(b) for b in _collect_stream(ls.GetBlockRange(req))]
        assert_equal(z_blocks, l_blocks)

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
        z_blocks = [_normalize_compact_block(b) for b in _collect_stream(zs.GetBlockRangeNullifiers(req))]
        l_blocks = [_normalize_compact_block(b) for b in _collect_stream(ls.GetBlockRangeNullifiers(req))]
        assert_equal(z_blocks, l_blocks)

    def test_get_block_range_nullifiers_reverse(self, zs, ls):
        req = service_pb2.BlockRange(
            start=service_pb2.BlockID(height=10, hash=b""),
            end=service_pb2.BlockID(height=1, hash=b""),
        )
        z_blocks = [_normalize_compact_block(b) for b in _collect_stream(zs.GetBlockRangeNullifiers(req))]
        l_blocks = [_normalize_compact_block(b) for b in _collect_stream(ls.GetBlockRangeNullifiers(req))]
        assert_equal(z_blocks, l_blocks)

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

    def test_get_taddress_txids_lower(self, zs, ls):
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

    def test_get_taddress_txids_upper(self, zs, ls):
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
    # Shielded coinbase tests
    #
    # These blocks were mined by zcashd with a Sapling miner address (ZIP 213,
    # active via Heartwood from block 1).  The coinbase output is a Sapling note,
    # so the compact block's vtx must be non-empty and identical across both
    # implementations — unlike transparent-only blocks where Zainod omits vtx.
    # -------------------------------------------------------------------------

    def test_get_block_shielded(self, zs, ls):
        """A shielded-coinbase block must have matching, non-empty vtx."""
        req = service_pb2.BlockID(height=self.shielded_block_height, hash=b"")
        z = _normalize_shielded_compact_block(zs.GetBlock(req))
        l = _normalize_shielded_compact_block(ls.GetBlock(req))
        assert_true(len(z.vtx) > 0, "Zainod returned empty vtx for shielded coinbase block")
        assert_true(len(l.vtx) > 0, "Lightwalletd returned empty vtx for shielded coinbase block")
        assert_equal(z, l)

    def test_get_block_nullifiers_shielded(self, zs, ls):
        req = service_pb2.BlockID(height=self.shielded_block_height, hash=b"")
        z = _normalize_shielded_compact_block(zs.GetBlockNullifiers(req))
        l = _normalize_shielded_compact_block(ls.GetBlockNullifiers(req))
        assert_equal(z, l)

    def test_get_block_range_shielded(self, zs, ls):
        """All blocks in the shielded range must have matching, non-empty vtx."""
        end = self.shielded_block_height + 5
        req = service_pb2.BlockRange(
            start=service_pb2.BlockID(height=self.shielded_block_height, hash=b""),
            end=service_pb2.BlockID(height=end, hash=b""),
        )
        z_blocks = [_normalize_shielded_compact_block(b) for b in _collect_stream(zs.GetBlockRange(req))]
        l_blocks = [_normalize_shielded_compact_block(b) for b in _collect_stream(ls.GetBlockRange(req))]
        assert_equal(len(z_blocks), len(l_blocks))
        for z_b, l_b in zip(z_blocks, l_blocks):
            assert_true(len(z_b.vtx) > 0, "Zainod returned empty vtx for shielded block at height %d" % z_b.height)
            assert_true(len(l_b.vtx) > 0, "Lightwalletd returned empty vtx for shielded block at height %d" % l_b.height)
            assert_equal(z_b, l_b)

    def test_get_transaction_shielded(self, zs, ls):
        """Shielded coinbase transaction bytes and height must match."""
        txid_bytes = bytes.fromhex(self.shielded_coinbase_txid)[::-1]
        req = service_pb2.TxFilter(hash=txid_bytes)
        z = zs.GetTransaction(req)
        l = ls.GetTransaction(req)
        assert_equal(z.data, l.data)
        assert_equal(z.height, l.height)
        assert_equal(z.height, self.shielded_block_height)

    def test_get_tree_state_sapling(self, zs, ls):
        """After Sapling coinbase the Sapling tree must be non-empty and identical."""
        req = service_pb2.BlockID(height=self.shielded_block_height, hash=b"")
        z = zs.GetTreeState(req)
        l = ls.GetTreeState(req)
        assert_equal(z.network, l.network)
        assert_equal(z.height, l.height)
        assert_equal(z.hash, l.hash)
        assert_equal(z.saplingTree, l.saplingTree)
        assert_equal(z.orchardTree, l.orchardTree)
        assert_true(len(z.saplingTree) > 0, "Sapling tree is empty after Sapling coinbase")

    def test_get_block_orchard(self, zs, ls):
        """An Orchard-coinbase block must have matching, non-empty vtx."""
        req = service_pb2.BlockID(height=self.orchard_block_height, hash=b"")
        z = _normalize_shielded_compact_block(zs.GetBlock(req))
        l = _normalize_shielded_compact_block(ls.GetBlock(req))
        assert_true(len(z.vtx) > 0, "Zainod returned empty vtx for Orchard coinbase block")
        assert_true(len(l.vtx) > 0, "Lightwalletd returned empty vtx for Orchard coinbase block")
        assert_equal(z, l)

    def test_get_transaction_orchard(self, zs, ls):
        """Orchard coinbase transaction bytes and height must match."""
        txid_bytes = bytes.fromhex(self.orchard_coinbase_txid)[::-1]
        req = service_pb2.TxFilter(hash=txid_bytes)
        z = zs.GetTransaction(req)
        l = ls.GetTransaction(req)
        assert_equal(z.data, l.data)
        assert_equal(z.height, l.height)
        assert_equal(z.height, self.orchard_block_height)

    def test_get_tree_state_orchard(self, zs, ls):
        """After Orchard coinbase both the Sapling and Orchard trees must be non-empty."""
        req = service_pb2.BlockID(height=self.orchard_block_height, hash=b"")
        z = zs.GetTreeState(req)
        l = ls.GetTreeState(req)
        assert_equal(z.network, l.network)
        assert_equal(z.height, l.height)
        assert_equal(z.hash, l.hash)
        assert_equal(z.saplingTree, l.saplingTree)
        assert_equal(z.orchardTree, l.orchardTree)
        assert_true(len(z.saplingTree) > 0, "Sapling tree is empty at Orchard coinbase block")
        assert_true(len(z.orchardTree) > 0, "Orchard tree is empty after Orchard coinbase")


if __name__ == '__main__':
    GrpcComparisonTest().main()
