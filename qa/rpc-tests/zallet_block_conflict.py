#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Regression test for zcash/zallet#556 / zcash/zallet#560: a `BlockConflict`
# (the backend's block hash at a height disagreeing with what's already
# stored in the wallet's own database) must not crash the whole wallet — it
# should search for the fork point and recover automatically instead.
#
# We can't reliably force this exact code path via a *natural* chain reorg:
# zallet's own proactive fork-point check runs before every scan and already
# resolves ordinary reorgs on its own, before a raw storage conflict could
# ever occur. Instead this reproduces the bug the same way it was originally
# verified by hand: stop the wallet, plant a deliberately-wrong block hash
# directly in its `wallet.db` at a height just ahead of its synced tip, then
# let it resume syncing into that conflict.
#

import os
import sqlite3
import time

from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import (
    assert_equal,
    start_wallet,
    wallet_dir,
    zallet_processes,
)


def _wait_for_wallet_sync(node, wallet, timeout=60):
    """Block until the wallet reports the node's current tip."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        target = node.getblockcount()
        status = wallet.getwalletstatus()
        if status.get('wallet_tip', {}).get('height') == target:
            return
        time.sleep(0.5)
    raise AssertionError("wallet did not sync to node tip within %ss" % timeout)


class ZalletBlockConflictTest(BitcoinTestFramework):
    def __init__(self):
        super().__init__()
        self.cache_behavior = 'clean'
        self.num_nodes = 1
        self.num_wallets = 1

    def run_test(self):
        node = self.nodes[0]
        wallet = self.wallets[0]

        # Get the wallet synced up a few blocks.
        node.generate(5)
        _wait_for_wallet_sync(node, wallet)
        synced_height = wallet.getwalletstatus()['wallet_tip']['height']
        assert_equal(synced_height, node.getblockcount())

        # Mine one more real block that the wallet hasn't scanned yet — this
        # is the height we'll plant a conflicting record at ahead of time.
        conflict_height = synced_height + 1
        node.generate(1)
        real_hash = node.getblockhash(conflict_height)
        assert_equal(node.getblockcount(), conflict_height)

        # Stop the wallet cleanly and wait for the process to fully exit
        # (the RPC call itself just requests shutdown) before touching its
        # database file directly, so we don't race its own file handle.
        wallet.stop()
        zallet_processes[0].wait(timeout=30)

        db_path = os.path.join(wallet_dir(self.options.tmpdir, 0), "wallet.db")
        conn = sqlite3.connect(db_path)
        try:
            # The non-height/hash columns (tree state, output/action counts,
            # etc.) must be a validly-consistent set, not arbitrary/empty
            # values, or the wallet fails on an unrelated deserialization
            # error before it ever gets to the hash check we're actually
            # testing. Clone the real, already-synced tip's row wholesale and
            # only override height and hash.
            columns = [
                row[1] for row in conn.execute("PRAGMA table_info(blocks)")
                if row[1] not in ("height", "hash")
            ]
            real_row = conn.execute(
                "SELECT %s FROM blocks WHERE height = ?" % ", ".join(columns),
                (synced_height,),
            ).fetchone()

            fake_hash = bytes([0xFF] * 32)
            conn.execute(
                "INSERT INTO blocks (height, hash, %s) VALUES (?, ?, %s)" % (
                    ", ".join(columns), ", ".join("?" * len(columns)),
                ),
                (conflict_height, fake_hash, *real_row),
            )
            conn.commit()
        finally:
            conn.close()

        # Restart the wallet. It should hit the planted conflict as soon as
        # it tries to scan `conflict_height`, log a recovery warning instead
        # of crashing, and resync cleanly to the node's real tip.
        restarted_wallet = start_wallet(0, self.options.tmpdir)
        self.wallets[0] = restarted_wallet

        _wait_for_wallet_sync(node, restarted_wallet, timeout=60)

        final_status = restarted_wallet.getwalletstatus()
        assert_equal(final_status['wallet_tip']['height'], node.getblockcount())
        assert_equal(final_status['wallet_tip']['blockhash'], real_hash)


if __name__ == '__main__':
    ZalletBlockConflictTest().main()
