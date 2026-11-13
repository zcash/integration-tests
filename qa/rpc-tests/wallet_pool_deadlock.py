#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Regression test for zcash/zallet#708: wallet RPCs must not deadlock on the
# wallet database connection pool while sync retains its long-lived
# connections.
#
# Zallet's sync tasks hold pooled database connections for the lifetime of the
# process. Before the fix, many wallet RPCs held one pooled connection while
# acquiring another (directly, or via the keystore), so on a small host whose
# CPU-derived pool sizing was close to what sync retains, wallet RPCs starved
# forever. On a fresh wallet this was a bootstrapping deadlock: with no
# accounts the wallet's recorded height never advances, and `z_getnewaccount`,
# the call that would create the account sync needs, was itself affected.
#
# This test starts a wallet WITHOUT an account (overriding the framework's
# account pre-creation), mines to the node's own default miner address so the
# wallet has nothing to scan for, and then drives the bootstrapping sequence
# and a burst of concurrent keystore-touching RPCs. Every RPC is wrapped in a
# deadline so a regression fails this test rather than hanging the suite.
#

import http.client
import socket
import threading
import time

from test_framework.test_framework import BitcoinTestFramework
from test_framework.authproxy import JSONRPCException
from test_framework.config import ZalletArgs
from test_framework.util import (
    assert_equal,
    assert_true,
    get_rpc_auth_proxy,
    rpc_url_wallet,
    start_wallets,
    wait_for_wallet_sync,
)

# zallet's sync engine permanently retains this many pooled database connections
# (`LONG_LIVED_SYNC_CONNECTIONS` in zallet-core). Keep in step if that constant changes.
SYNC_CONNECTIONS = 4

# Force the wallet's connection pool to one connection above what sync retains, leaving
# exactly one for foreground RPC work. The production pool is CPU-derived and floored well
# above this, which would let a reintroduced deadlock pass unnoticed on a many-core CI
# host; pinning it this low makes a regression deadlock deterministically anywhere. With
# one foreground connection, the fixed code (which releases a connection before acquiring
# another) still makes progress serially, while the #708 bug (holding one while acquiring
# a second) wedges on the very first such call.
MAX_CONNECTIONS = SYNC_CONNECTIONS + 1

# An RPC that deadlocks on the connection pool never answers at all (the
# pool's acquisition has no timeout), so any finite bound separates a deadlock
# from a slow machine. Transient transport timeouts are retried below until
# this deadline, matching how the framework treats a wallet that is merely
# busy.
RPC_DEADLINE = 120

# Per-attempt HTTP timeout for every proxy this test creates. It must be well
# below RPC_DEADLINE so that several attempts fit inside the deadline (the
# framework's default of 600s would let a single wedged attempt blow straight
# through it), while staying far above the cost of the quick wallet reads these
# calls perform on a live wallet.
RPC_ATTEMPT_TIMEOUT = 25

# zcashd's RPC in-warmup error code, which Zallet reuses for "Wallet sync
# required". It is a well-formed answer (the opposite of a deadlock), but the
# bootstrapping sequence retries it: on a fresh wallet it only means the sync
# engine has not yet recorded a chain height to base an account birthday on.
RPC_IN_WARMUP = -28

# The concurrent phase fires many more simultaneous wallet RPCs than the single
# foreground connection this test leaves free, so correctly staged calls must queue and
# complete one at a time rather than wedge the pool.
CONCURRENT_CALLS = 12


class WalletPoolDeadlockTest(BitcoinTestFramework):

    def __init__(self):
        super().__init__()
        self.num_nodes = 1
        self.num_wallets = 1
        self.cache_behavior = 'clean'

    def prepare_wallets(self):
        # Deliberately do NOT run the framework's account pre-creation
        # (`regtest generate-account-and-miner-address`): the point of the
        # bootstrapping phase is a wallet with a mnemonic but no accounts.
        # Leaving `miner_addresses` unset makes the node mine to its default
        # miner address, so no coinbase ever belongs to this wallet.
        # `start_wallet` initializes encryption and the mnemonic itself when
        # it sees a fresh datadir.
        pass

    def setup_wallets(self):
        # Start the wallet with a deliberately small connection pool (see
        # MAX_CONNECTIONS) so a pool-exhaustion deadlock reproduces regardless of the CI
        # host's CPU count. The config is written before the wallet database is created
        # and before `zallet start`, so the pool is this size from the first open.
        zallet_args = [
            ZalletArgs(max_connections=MAX_CONNECTIONS)
            for _ in range(self.num_wallets)
        ]
        return start_wallets(
            self.num_wallets, self.options.tmpdir, zallet_args=zallet_args)

    def call_within_deadline(self, wallet, method, *args, retry_warmup=False):
        """Calls a wallet RPC, retrying transport timeouts (and, optionally,
        the in-warmup refusal) until RPC_DEADLINE.

        Returns the RPC result. Any other JSON-RPC error propagates: an error
        response is an answer, and callers assert on answers. Only the
        complete absence of an answer by the deadline is reported as the
        pool-deadlock regression this test exists to catch.
        """
        deadline = time.time() + RPC_DEADLINE
        last_error = None
        while time.time() < deadline:
            try:
                return getattr(wallet, method)(*args)
            except JSONRPCException as e:
                if retry_warmup and e.error['code'] == RPC_IN_WARMUP:
                    last_error = e
                    time.sleep(0.5)
                    continue
                raise
            except (socket.timeout, TimeoutError, OSError,
                    http.client.HTTPException) as e:
                # The wallet accepted the request but did not answer within
                # the proxy's HTTP timeout, or the connection dropped. A busy
                # wallet answers on a retry; a deadlocked one never does.
                last_error = e
                time.sleep(0.5)
        raise AssertionError(
            "%s did not answer within %ds; the wallet has deadlocked on its "
            "database connection pool (zcash/zallet#708). Last error: %r"
            % (method, RPC_DEADLINE, last_error))

    def run_test(self):
        node = self.nodes[0]
        # A dedicated proxy with a short per-attempt timeout (see
        # RPC_ATTEMPT_TIMEOUT): the framework proxy's 600s default would make a
        # wedged attempt outlast RPC_DEADLINE, so the deadline would be
        # enforced by the HTTP timeout rather than by call_within_deadline.
        wallet = get_rpc_auth_proxy(
            rpc_url_wallet(0), 0, timeout=RPC_ATTEMPT_TIMEOUT)

        # Give the chain a tip well above the wallet's (empty) scan progress.
        # All coinbase goes to the node's default miner address, so the wallet
        # stays at its bootstrapping state: nothing to scan, no accounts.
        node.generate(5)

        # --- Phase 1: the bootstrapping sequence from the issue report. ---

        # Read-only and keystore-touching RPCs must answer on the accountless
        # wallet. `getwalletinfo` reads the keystore's database tables, which
        # is one of the acquisitions that used to starve.
        accounts = self.call_within_deadline(wallet, 'z_listaccounts')
        assert_equal(accounts, [])

        info = self.call_within_deadline(wallet, 'getwalletinfo')
        assert_true('walletversion' in info or len(info) > 0,
                    "getwalletinfo should answer with wallet information")

        # The call that unblocks bootstrapping: it reads the wallet's chain
        # height, selects and decrypts the seed in the keystore, and creates
        # the account. Retried on in-warmup only, since the fresh wallet may
        # not have recorded a chain height yet.
        account = self.call_within_deadline(
            wallet, 'z_getnewaccount', 'pool-708', retry_warmup=True)
        account_uuid = account['account_uuid']

        accounts = self.call_within_deadline(wallet, 'z_listaccounts')
        assert_equal(len(accounts), 1)
        assert_equal(accounts[0]['account_uuid'], account_uuid)

        # Deriving an address resolves the account through both the keystore
        # and the wallet database.
        addr_result = self.call_within_deadline(
            wallet, 'z_getaddressforaccount', account_uuid,
            retry_warmup=True)
        unified_address = addr_result['address']
        assert_true(len(unified_address) > 0,
                    "z_getaddressforaccount should return an address")

        # With an account in place, sync must now make progress to the tip:
        # the other half of the bootstrapping deadlock was that the wallet
        # height never advanced. Use the framework proxy (long HTTP timeout)
        # here: `wait_for_wallet_sync` does not retry transport timeouts, so
        # the short-timeout probe proxy could abort the wait on one slow poll.
        wait_for_wallet_sync(node, self.wallets[0], timeout=RPC_DEADLINE)

        # --- Phase 2: concurrent keystore-touching RPCs under sync. ---

        # Mine more blocks so sync has ongoing work while the burst runs,
        # then fire more simultaneous wallet RPCs than the pool keeps free
        # for foreground work. Each call mixes wallet-database reads with
        # keystore access; all of them must answer.
        node.generate(5)

        methods = [
            ('getwalletinfo', ()),
            ('z_listaccounts', ()),
            ('z_getaddressforaccount', (account_uuid,)),
            ('z_exportviewingkey', (unified_address,)),
        ]
        failures = []
        threads = []

        def burst_call(idx, method, args):
            # Each thread gets its own RPC connection: AuthServiceProxy is
            # not thread-safe, and sharing one would serialize the burst.
            proxy = get_rpc_auth_proxy(
                rpc_url_wallet(0), 0, timeout=RPC_ATTEMPT_TIMEOUT)
            try:
                self.call_within_deadline(proxy, method, *args,
                                          retry_warmup=True)
            except JSONRPCException:
                # An error response is still an answer: the property under
                # test is that no call wedges the pool.
                pass
            except AssertionError as e:
                failures.append((idx, method, str(e)))

        for i in range(CONCURRENT_CALLS):
            method, args = methods[i % len(methods)]
            t = threading.Thread(target=burst_call, args=(i, method, args))
            t.start()
            threads.append(t)
        for t in threads:
            # The deadline lives inside call_within_deadline; this join only
            # needs to outlast it.
            t.join(RPC_DEADLINE + 30)
            assert_true(not t.is_alive(),
                        "a concurrent RPC thread is still running after its "
                        "deadline; the wallet has deadlocked (zallet#708)")
        assert_equal(failures, [])

        # The wallet is still live and consistent after the burst.
        accounts = self.call_within_deadline(wallet, 'z_listaccounts')
        assert_equal(len(accounts), 1)

        print("Pool-deadlock regression checks passed")


if __name__ == '__main__':
    WalletPoolDeadlockTest().main()
