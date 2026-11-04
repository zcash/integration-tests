#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# `z_sendmany`'s privacy policy, enforced against the account's real per-pool
# balances, against the Z3 stack (zebrad + zaino + zallet).
#
# Before proposing a transfer, zallet asks of each recipient: can this payment be
# funded without crossing between shielded pools? A crossing shows up in the
# transaction's public value balances, so it reveals the amount that crossed, and
# needs `AllowRevealedAmounts` or weaker. Under the default `FullPrivacy` the send
# must be refused, and refused FOR THAT REASON -- naming the policy that would
# permit it -- rather than as a generic failure to build a transaction.
#
# zallet used to ask that question against MAX_MONEY in every pool, which is
# always enough, so it never refused for this reason: the conflict surfaced only
# after input selection had run, either as a different privacy error raised
# against the proposal or as a bare "failed to propose transaction". This covers
# the fix (zcash/zallet#257), in both directions:
#
#   1. A payment no single shielded pool can cover is refused under FullPrivacy
#      (phases 1-3), and permitted once the caller allows revealed amounts
#      (phase 4) -- so the refusal is about the policy, not about the funds.
#
#   2. A payment one pool CAN cover on its own still goes through under
#      FullPrivacy, and arrives (phase 5). This is the anti-regression
#      direction: reading real balances must not start refusing sends that
#      reveal nothing.
#
# `pczt_create` shares the proposal path with `z_sendmany`, so phase 3 checks it
# too: a policy enforced on one and not the other would let a caller build the
# very transaction it had just been refused.
#
# The pools are funded by shielding matured coinbase, which is the only way to
# get value into a shielded pool on a fresh chain (consensus requires coinbase be
# spent to a single shielded output, and `z_sendmany` will not select it).
# Amounts are then derived from the balances READ BACK, not computed, so this
# test does not depend on the block subsidy, on the fee schedule, or on whether
# the shielded coinbase lands in Orchard or in Ironwood.
#

import time
from decimal import Decimal

from test_framework.config import ZebraArgs
from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import (
    COIN,
    COINBASE_MATURITY,
    INTERNAL_FEE,
    MIN_CONFIRMATIONS,
    Pool,
    PrivacyPolicy,
    Receiver,
    assert_equal,
    assert_in_message,
    assert_true,
    expect_rpc_error,
    nu_activation_all_at_1,
    start_nodes,
    wait_and_assert_operationid_status,
    wait_for_account_spendable,
    wait_for_mature_coinbase_count,
    wait_for_tx_scanned,
    zat,
)

# Blocks mined past the maturity depth, so several mature coinbase outputs are
# available to shield into each pool.
SPARE_BLOCKS = 20

# Mature coinbase outputs swept into each shielded pool. Two, so that each pool
# is funded well enough to pay from on its own, while a payment sized above
# either one still fits inside the two combined.
UTXOS_PER_POOL = 2

# How far above a pool's balance to place a payment that pool cannot cover, and
# the size of the single-pool payment in phase 5. Fees are orders of magnitude
# smaller, so `balance + MARGIN` is beyond that pool's reach whatever fee the
# builder settles on.
MARGIN = Decimal('0.01')

# The distinguishing clause of each rejection. The rendered messages wrap across
# several lines and carry a trailing recommendation sentence, so matching them
# whole would break on rewording that does not change what was refused.
CROSSING_POOLS = 'which would reveal transaction amounts'
UNIFIED_RECEIVER = 'Could not send to a shielded receiver of a unified address'
WEAKEN_TO_REVEALED_AMOUNTS = "'AllowRevealedAmounts' or weaker"


class WalletZSendmanyPrivacyTest(BitcoinTestFramework):

    def __init__(self):
        super().__init__()
        self.num_nodes = 1
        self.num_wallets = 1
        self.cache_behavior = 'clean'

    def setup_nodes(self):
        # Every later NU must activate at height 1 as well, or zebra mines a
        # coinbase committing to a branch ID that zallet's network params do not
        # expect, and the first block fails to sync.
        args = [
            ZebraArgs(
                miner_address=addr,
                activation_heights=nu_activation_all_at_1(),
            ) for addr in self.miner_addresses
        ]
        return start_nodes(self.num_nodes, self.options.tmpdir, args)

    def shielded_balances(self, wallet, account_uuid):
        """The account's spendable Sapling balance, and what can pay an Orchard
        receiver, both in zatoshis.

        This test runs with NU6.3 inactive (see `setup_nodes`), so an Orchard
        receiver is paid out of the Orchard pool and the Orchard balance is what
        covers it. Ironwood is deliberately NOT added in: the two are distinct
        value pools, and once NU6.3 activates it is Ironwood rather than Orchard
        that pays an Orchard receiver, with a crossing between them revealing
        its amount. `wallet_z_sendmany_privacy_ironwood.py` covers that era.
        """
        pools = wallet.z_getbalanceforaccount(
            account_uuid, MIN_CONFIRMATIONS)['pools']

        def spendable(pool):
            # A pool holding nothing is omitted from the response entirely.
            return pools[pool]['valueZat'] if pool in pools else 0

        assert_equal(
            spendable(Pool.IRONWOOD), 0,
            "NU6.3 is inactive here, so no funds should reach the Ironwood pool")

        return spendable(Pool.SAPLING), spendable(Pool.ORCHARD)

    def wait_for_shielded_balances(self, wallet, account_uuid, timeout=240):
        """Poll until both shielded sides hold spendable funds, then return them.

        A shielded note becomes spendable only once its witness has been built,
        which lands after the confirming transaction itself is scanned, so the
        funded balance has to be polled rather than read once.

        Both sides are read through `shielded_balances`, which also asserts that
        the Ironwood pool stays empty, so a chain that unexpectedly activated
        NU6.3 fails here rather than silently changing what the test covers.
        """
        deadline = time.time() + timeout
        while True:
            sapling, orchard = self.shielded_balances(wallet, account_uuid)
            if (sapling > 0 and orchard > 0) or time.time() >= deadline:
                # On timeout, return what was last seen so the caller's
                # assertion can report the actual balances.
                return sapling, orchard
            time.sleep(2)

    def fund_pool(self, wallet, node, taddr, toaddress, expected_mature):
        """Sweep `UTXOS_PER_POOL` matured coinbase outputs into whichever pool
        `toaddress` receives into.

        `expected_mature` is how many mature coinbase UTXOs the wallet should be
        holding: the sweep waits for its view to settle on that count first,
        because z_listunspent sees a new tip before the proposal builder's
        spendable view does.
        """
        wait_for_mature_coinbase_count(wallet, expected_mature)
        result = wallet.z_shieldcoinbase(
            taddr, toaddress, INTERNAL_FEE, UTXOS_PER_POOL)
        assert_equal(result['shieldingUTXOs'], UTXOS_PER_POOL)

        txid = wait_and_assert_operationid_status(wallet, result['opid'])
        assert_true(txid is not None, "shielding should have produced a transaction")
        node.generate(1)
        wait_for_tx_scanned(wallet, txid)
        return txid

    def run_test(self):
        node = self.nodes[0]
        wallet = self.wallets[0]
        taddr = self.miner_addresses[0]

        # Account-mutating RPCs need a chain height to anchor a birthday to, so
        # the wallet must have committed a block before they are called.
        self.sync_all()
        sender = wallet.z_listaccounts()[0]['account_uuid']

        # Recipients live in a second account, so that a payment is a real
        # transfer rather than an account paying itself.
        recipient = wallet.z_getnewaccount("privacy-test-recipient")['account_uuid']

        # A bare Sapling address names one pool: it can be paid out of Sapling,
        # or by crossing into Sapling from elsewhere and revealing the amount.
        recipient_sapling_ua = wallet.z_getaddressforaccount(
            recipient, [Receiver.SAPLING])['address']
        recipient_sapling = wallet.z_listunifiedreceivers(
            recipient_sapling_ua)[Receiver.SAPLING]

        # A unified address with BOTH shielded receivers can be paid from either
        # pool, so it is only forced to cross when neither covers the payment.
        recipient_dual = wallet.z_getaddressforaccount(
            recipient, [Receiver.SAPLING, Receiver.ORCHARD])['address']

        print("Mining blocks to mature coinbase...")
        node.generate(COINBASE_MATURITY + SPARE_BLOCKS)
        self.sync_all()

        print("Funding the sender's Sapling and Orchard pools from coinbase...")
        sender_sapling_ua = wallet.z_getaddressforaccount(
            sender, [Receiver.SAPLING])['address']
        sender_orchard_ua = wallet.z_getaddressforaccount(
            sender, [Receiver.ORCHARD])['address']
        # Every block pays its coinbase to the miner address, so the count of
        # mature outputs is a function of the tip and of how many have been
        # swept already.
        mature_spent = 0

        def expected_mature():
            return node.getblockcount() - COINBASE_MATURITY + 1 - mature_spent

        self.fund_pool(wallet, node, taddr, sender_sapling_ua, expected_mature())
        mature_spent += UTXOS_PER_POOL
        self.fund_pool(wallet, node, taddr, sender_orchard_ua, expected_mature())
        mature_spent += UTXOS_PER_POOL

        sapling, orchard = self.wait_for_shielded_balances(wallet, sender)
        print("  sender holds {} zat in Sapling, {} zat payable to an Orchard "
              "receiver".format(sapling, orchard))
        assert_true(sapling > 0, "the Sapling pool must be funded")
        assert_true(orchard > 0, "the Orchard-payable pools must be funded")

        # Above either pool alone, below the two together: paying this has to
        # cross between pools, and so reveals the amount that crossed.
        needs_crossing = max(sapling, orchard) + zat(MARGIN)
        assert_true(
            needs_crossing < sapling + orchard,
            "the payment must be affordable once both pools are combined")

        def zec(zatoshis):
            """A zatoshi count as the ZEC amount an RPC argument takes."""
            return Decimal(zatoshis) / Decimal(COIN)

        beyond_sapling = zec(sapling + zat(MARGIN))
        crossing_payment = [{'address': recipient_dual,
                             'amount': zec(needs_crossing)}]

        print("Phase 1: a Sapling recipient the Sapling pool cannot cover is refused...")
        # Both under the default policy and under FullPrivacy named explicitly:
        # the default IS FullPrivacy, and a caller relying on that must get the
        # same protection as one that spells it out.
        #
        # Note this phase does not by itself distinguish the pre-flight from the
        # post-proposal check: a bare Sapling recipient can only be paid by
        # crossing INTO Sapling, so both checks report a crossing into the same
        # pool and the messages coincide. Phases 2 and 3 are what separate them,
        # and they are the ones that fail without the balances being read.
        for policy in [None, PrivacyPolicy.FULL_PRIVACY]:
            e = expect_rpc_error(
                wallet.z_sendmany,
                sender_sapling_ua,
                [{'address': recipient_sapling, 'amount': beyond_sapling}],
                MIN_CONFIRMATIONS,
                INTERNAL_FEE,
                policy,
            )
            assert_in_message(e, CROSSING_POOLS)
            # The refusal has to say what would let the send through, or the
            # caller has nothing to act on.
            assert_in_message(e, WEAKEN_TO_REVEALED_AMOUNTS)

        print("Phase 2: a unified recipient no single pool can cover is refused...")
        e = expect_rpc_error(
            wallet.z_sendmany,
            sender_sapling_ua,
            crossing_payment,
            MIN_CONFIRMATIONS,
            INTERNAL_FEE,
            PrivacyPolicy.FULL_PRIVACY,
        )
        assert_in_message(e, UNIFIED_RECEIVER)
        assert_in_message(e, WEAKEN_TO_REVEALED_AMOUNTS)

        print("Phase 3: pczt_create applies the same check...")
        e = expect_rpc_error(
            wallet.pczt_create,
            sender_sapling_ua,
            crossing_payment,
            MIN_CONFIRMATIONS,
            PrivacyPolicy.FULL_PRIVACY,
        )
        assert_in_message(e, UNIFIED_RECEIVER)

        print("Phase 4: a payment one pool covers is still permitted under FullPrivacy...")
        # The anti-regression case: reading real balances must not start refusing
        # sends that reveal nothing. It runs before the crossing send below, which
        # draws on both pools and would leave nothing to pay from.
        before, _ = self.shielded_balances(wallet, recipient)
        opid = wallet.z_sendmany(
            sender_sapling_ua,
            [{'address': recipient_sapling, 'amount': MARGIN}],
            MIN_CONFIRMATIONS,
            INTERNAL_FEE,
            PrivacyPolicy.FULL_PRIVACY,
        )
        txid = wait_and_assert_operationid_status(wallet, opid)
        assert_true(txid is not None, "a single-pool payment should succeed")
        node.generate(1)
        wait_for_tx_scanned(wallet, txid)

        wait_for_account_spendable(
            wallet, recipient, Pool.SAPLING, min_zat=before + zat(MARGIN))
        after, _ = self.shielded_balances(wallet, recipient)
        assert_equal(after, before + zat(MARGIN))
        print("  payment arrived in the recipient's Sapling pool")

        print("Phase 5: the refused send is permitted once revealed amounts are allowed...")
        # Nothing has changed except what the caller said it would accept, which
        # is what shows the refusals in phases 1-3 were about the policy and not
        # about the funds. The payment is re-sized against the balances as they
        # stand now, since phase 4 spent from Sapling.
        sapling, orchard = self.wait_for_shielded_balances(wallet, sender)
        needs_crossing = max(sapling, orchard) + zat(MARGIN)
        assert_true(
            needs_crossing < sapling + orchard,
            "the payment must still be affordable once both pools are combined")

        opid = wallet.z_sendmany(
            sender_sapling_ua,
            [{'address': recipient_dual, 'amount': zec(needs_crossing)}],
            MIN_CONFIRMATIONS,
            INTERNAL_FEE,
            PrivacyPolicy.ALLOW_REVEALED_AMOUNTS,
        )
        txid = wait_and_assert_operationid_status(wallet, opid)
        assert_true(txid is not None, "the send should have produced a transaction")
        node.generate(1)
        wait_for_tx_scanned(wallet, txid)
        print("  cross-pool send accepted under AllowRevealedAmounts. PASSED")


if __name__ == '__main__':
    WalletZSendmanyPrivacyTest().main()
