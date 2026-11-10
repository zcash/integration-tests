#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Regression test for https://github.com/zcash/zallet/issues/695:
# `z_listunspent` must not fail the whole request when the wallet holds an
# unspent Ironwood note whose memo cannot be decoded.
#
# zebrad's `generate` randomizes the shielded coinbase memo with 512 random
# bytes before each block. A memo whose first byte is <= 0xF4 is ZIP 302 text
# but is almost never valid UTF-8, so the wallet's typed decode fails. Before
# the fix, `z_listunspent` propagated that error and failed with
# "WalletDb::get_memo failed", hiding every other unspent output.
#
# On a wallet holding both transparent coinbase and `generate`-mined Ironwood
# coinbase (the mixed-pool case from the issue), this checks that the Ironwood
# notes are reported with their memo fields omitted (never null), and that the
# transparent outputs are not lost. First bytes 0xF5 and 0xF7..0xFF decode as
# ZIP 302 future/arbitrary memos and legitimately keep a `memo`, so the test
# requires at least one note without memo fields rather than all of them.
#

from test_framework.test_framework import BitcoinTestFramework
from test_framework.config import ZebraArgs, ZalletArgs
from test_framework.util import (
    COINBASE_MATURITY,
    Pool,
    assert_true,
    bitcoind_processes,
    nu_activation_all_at_1_with_ironwood,
    start_node,
    start_nodes,
    start_wallet,
    start_wallets,
    zallet_processes,
)


class WalletIronwoodMemoTest(BitcoinTestFramework):

    def __init__(self):
        super().__init__()
        self.num_nodes = 1
        self.num_wallets = 1
        self.cache_behavior = 'clean'
        # Both the node and the wallet must activate NU6.3 at the same height;
        # see wallet_ironwood.py.
        self.activation_heights = nu_activation_all_at_1_with_ironwood()

    def setup_nodes(self):
        args = [
            ZebraArgs(
                miner_address=addr,
                activation_heights=self.activation_heights,
            ) for addr in self.miner_addresses
        ]
        return start_nodes(self.num_nodes, self.options.tmpdir, args)

    def setup_wallets(self):
        zallet_args = [
            ZalletArgs(activation_heights=self.activation_heights)
            for _ in range(self.num_wallets)
        ]
        return start_wallets(
            self.num_wallets, self.options.tmpdir, zallet_args=zallet_args)

    def run_test(self):
        node = self.nodes[0]
        w = self.wallets[0]

        def patient(what, fn, tries=60, wait=5):
            # Retry transport failures and warmup errors; propagate every other
            # JSON-RPC error, including the #695 failure this test exercises.
            import time as _t
            from test_framework.authproxy import JSONRPCException
            last = None
            for _ in range(tries):
                try:
                    return fn()
                except JSONRPCException as e:
                    msg = e.error.get('message', '') if isinstance(
                        getattr(e, 'error', None), dict) else str(e)
                    if 'chain height is unknown' not in msg:
                        raise
                    last = e
                    _t.sleep(wait)
                except Exception as e:
                    last = e
                    _t.sleep(wait)
            raise AssertionError(
                "{} kept failing at transport level: {}".format(what, last))

        def restart_wallet():
            # Beta-era wallets shut down when their chain backend has trouble
            # ("steady-state sync task exited ... kind: Sync"). The database
            # persists, so bring a fresh wallet back up against the node.
            if zallet_processes[0].poll() is None:
                zallet_processes[0].terminate()
            zallet_processes[0].wait()
            self.wallets[0] = start_wallet(
                0, self.options.tmpdir,
                zallet_args=ZalletArgs(
                    activation_heights=self.activation_heights))
            return self.wallets[0]

        # Phase 1: transparent coinbase. The mixed-pool check needs a mature
        # transparent output; immature transparent coinbase is not listed,
        # while immature Ironwood coinbase (phase 2) is.
        print("Phase 1: mining transparent coinbase...")
        node.generate(COINBASE_MATURITY + 2)
        acct = patient("z_listaccounts",
                       lambda: w.z_listaccounts())[0]['account_uuid']
        ua = patient("z_getaddressforaccount",
                     lambda: w.z_getaddressforaccount(acct, ['orchard']))['address']

        # Phase 2: restart zebrad mining to the wallet's Orchard UA, so each
        # `generate`d block mints an Ironwood coinbase note with a random memo.
        print("Phase 2: restarting zebrad with a shielded miner address...")
        self.nodes[0].stop()
        bitcoind_processes[0].wait()
        self.nodes[0] = start_node(0, self.options.tmpdir, ZebraArgs(
            miner_address=ua,
            activation_heights=self.activation_heights,
        ))
        node = self.nodes[0]

        # Mine while the old wallet is down (it died with its backend), then
        # bring a fresh wallet up against the mined chain.
        print("Phase 2: mining Ironwood coinbase with randomized memos...")
        node.generate(8)
        w = restart_wallet()

        # The unfixed wallet fails this call once an Ironwood note is scanned;
        # the fixed wallet returns it. Poll until the note is scanned.
        print("Calling unfiltered z_listunspent...")
        import time as _time
        deadline = _time.time() + 600
        while True:
            if zallet_processes[0].poll() is not None:
                w = restart_wallet()
            unspent = patient("z_listunspent", lambda: w.z_listunspent(1),
                              tries=12, wait=5)
            if any(u['pool'] == Pool.IRONWOOD for u in unspent):
                break
            assert _time.time() < deadline, \
                "wallet never scanned an Ironwood coinbase note; " \
                "last z_listunspent: {}".format(unspent)
            _time.sleep(5)

        ironwood = [u for u in unspent if u['pool'] == Pool.IRONWOOD]
        transparent = [u for u in unspent if u['pool'] == 'transparent']

        assert_true(
            len(ironwood) >= 1,
            "z_listunspent should report the mined Ironwood coinbase notes")
        assert_true(
            len(transparent) >= 1,
            "z_listunspent should still report transparent outputs when the "
            "wallet also holds Ironwood notes with undecodable memos")

        without_memo = 0
        for note in ironwood:
            if 'memo' in note:
                assert_true(note['memo'] is not None,
                            "memo must be omitted rather than null")
                assert_true(note.get('memoStr') is None or
                            isinstance(note['memoStr'], str),
                            "memoStr must be omitted or a string")
            else:
                assert_true('memoStr' not in note,
                            "memoStr must not be present without memo")
                without_memo += 1

        assert_true(
            without_memo >= 1,
            "at least one randomized Ironwood coinbase memo should be "
            "undecodable and render without memo fields; got {} notes, all "
            "with memos".format(len(ironwood)))

        print("PASSED: {} ironwood note(s) ({} without memo fields), "
              "{} transparent output(s)".format(
                  len(ironwood), without_memo, len(transparent)))


if __name__ == '__main__':
    WalletIronwoodMemoTest().main()
