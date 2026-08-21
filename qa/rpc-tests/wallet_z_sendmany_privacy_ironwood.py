#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# `z_sendmany`'s privacy policy across the NU6.3 Orchard turnstile, against the
# Z3 stack (zebrad + zaino + zallet).
#
# `wallet_z_sendmany_privacy.py` covers the pre-NU6.3 chain, where an Orchard
# receiver is paid out of the Orchard pool. This covers the era after NU6.3,
# where it is not.
#
# From NU6.3 the Orchard turnstile is one-way: value may leave the Orchard pool
# but never enter it. A payment to an Orchard receiver is therefore delivered
# through the Ironwood bundle and its value lands in the IRONWOOD pool, so only
# Ironwood funds can cover it without a pool crossing. Orchard funds reach that
# same recipient only by crossing, which shows the crossed amount in the
# transaction's public value balances. The recipient's address does not change:
# ZIP 316 has no Ironwood typecode, so an Ironwood note is received AT an Orchard
# receiver -- only the pool behind the receiver moves at activation.
#
# `wallet_ironwood_orchard_turnstile.py` pins those routing rules themselves.
# What it does not cover, and this does, is their consequence for the PRIVACY
# POLICY: it sends exclusively with `AllowRevealedAmounts`, so it never asks what
# the default `FullPrivacy` policy should refuse.
#
# The answer is that Orchard and Ironwood are distinct pools for policy purposes.
# Treating them as interchangeable -- summing the two balances when asking "can
# this recipient be paid without crossing?" -- is wrong in a way phase 1 detects:
#
#   * Correct: the Ironwood balance alone is compared against the payment, which
#     it cannot cover, so the PRE-FLIGHT refuses the send and reports the
#     unified-receiver conflict.
#
#   * Summing: Orchard + Ironwood covers the payment, so the pre-flight lets it
#     through and `enforce_privacy_policy` refuses the built proposal instead --
#     later, after input selection has run, and with a different message naming
#     the Ironwood pool. Phase 1 asserts that message is absent, so this test
#     fails against a build that sums them.
#
# NU6.3 is deferred rather than active from height 1, so a single wallet holds
# Orchard notes minted before activation and Ironwood notes minted after.
#
# Both shielded notes are minted at an exact size by paying the sender's own
# Orchard receiver out of Sapling, rather than by shielding coinbase into it
# directly: the amounts have to be known relative to each other (Orchard must
# cover a payment Ironwood cannot), and a many-UTXO coinbase shield straight into
# an Orchard receiver leaves the note unspendable anyway -- see the same
# construction in `wallet_ironwood_orchard_turnstile.py`.
#

import time
from decimal import Decimal

from test_framework.util import (
    COIN,
    COINBASE_MATURITY,
    INTERNAL_FEE,
    MIN_CONFIRMATIONS,
    AuthJSONRPCException,
    Pool,
    PrivacyPolicy,
    Receiver,
    account_spendable_zat,
    assert_equal,
    assert_in_message,
    assert_true,
    expect_rpc_error,
    nu_activation_ironwood_at,
    wait_account_settled,
    wait_and_assert_operationid_status,
    wait_for_account_spendable,
    wait_for_mature_coinbase_count,
    wait_for_tx_scanned,
    wait_for_wallet_sync,
    zat,
)
from test_framework.util_ironwood import IronwoodTestFramework

# The height NU6.3 activates at. Above the tip left behind by maturing and
# shielding coinbase in the Orchard era, so that era stays open long enough to
# mint a spendable Orchard note.
IRONWOOD_HEIGHT = 210

# Blocks mined past the maturity depth, so there are mature coinbase outputs to
# shield without the tip running into NU6.3.
SPARE_BLOCKS = 20

# Mature coinbase outputs swept into Sapling, the pool both shielded notes are
# then minted out of. Enough that the sweep comfortably covers both notes and
# their fees whatever the regtest subsidy has halved to.
SHIELD_UTXOS = 10

# Blocks mined past activation before the Ironwood era begins in earnest, so the
# wallet is unambiguously on the far side of the turnstile.
BLOCKS_PAST_ACTIVATION = 10

# The Orchard note minted before activation, and the Ironwood note minted after.
# Orchard is the larger of the two, so that a payment sized just above Ironwood
# is one Orchard could have covered -- which is what makes phase 1 discriminate
# between reading the two pools separately and summing them.
ORCHARD_FUNDING = Decimal('1.0')
IRONWOOD_FUNDING = Decimal('0.1')

# How far above the Ironwood balance to place a payment Ironwood cannot cover,
# and the size of the single-pool payment in phase 3. Fees are orders of
# magnitude smaller, so this is beyond Ironwood's reach whatever fee the builder
# settles on.
MARGIN = Decimal('0.01')

# The distinguishing clause of each rejection. The rendered messages wrap across
# several lines and carry a trailing recommendation sentence, so matching them
# whole would break on rewording that does not change what was refused.
UNIFIED_RECEIVER = 'Could not send to a shielded receiver of a unified address'
WEAKEN_TO_REVEALED_AMOUNTS = "'AllowRevealedAmounts' or weaker"

# The post-proposal message, which names the pool crossed INTO. Its presence
# means the pre-flight let the send through and the proposal was built and only
# then rejected -- the late failure this check exists to replace.
CROSSED_INTO_IRONWOOD = 'Could not send to the Ironwood shielded pool'


def assert_not_in_message(e, needle: str) -> None:
    """Assert that JSONRPCException `e`'s message does NOT contain `needle`."""
    msg = e.error['message']
    assert_true(
        needle not in msg,
        "Did not expect {!r} in error, got: {!r}".format(needle, msg))


def zec(zatoshis: int) -> Decimal:
    """A zatoshi count as the ZEC amount an RPC argument takes."""
    return Decimal(zatoshis) / Decimal(COIN)


def wait_for_balance_reads(wallet, timeout: int = 120) -> None:
    """Block until the wallet will answer balance queries at all.

    `wait_for_wallet_sync` waits for the wallet's reported tip, which it can
    reach while the scanned range backing the balances is still catching up; the
    balance RPCs then refuse outright with "still catching up". Polling the RPC
    this test depends on is the direct wait for the state it needs.
    """
    deadline = time.time() + timeout
    while True:
        try:
            wallet.z_getbalances(MIN_CONFIRMATIONS)
            return
        except AuthJSONRPCException:
            if time.time() >= deadline:
                raise
            time.sleep(1)


class WalletZSendmanyPrivacyIronwoodTest(IronwoodTestFramework):

    def __init__(self) -> None:
        super().__init__()
        # `IronwoodTestFramework` activates NU6.3 at height 1, which would leave
        # no era in which an Orchard receiver mints real Orchard notes. Deferring
        # it is what lets one wallet hold both kinds at once.
        self.activation_heights = nu_activation_ironwood_at(IRONWOOD_HEIGHT)

    def pool_balances(self, wallet, account_uuid):
        """The account's spendable Orchard and Ironwood balances, in zatoshis.

        Read separately and never summed: they are distinct value pools, and
        which one pays an Orchard receiver is exactly what NU6.3 changes.
        """
        return (account_spendable_zat(wallet, account_uuid, Pool.ORCHARD),
                account_spendable_zat(wallet, account_uuid, Pool.IRONWOOD))

    def send(self, node, wallet, from_addr, to_addr, amount, policy):
        """z_sendmany one recipient, confirm it, and return the txid."""
        opid = wallet.z_sendmany(
            from_addr,
            [{'address': to_addr, 'amount': amount}],
            MIN_CONFIRMATIONS,
            INTERNAL_FEE,
            policy,
        )
        txid = wait_and_assert_operationid_status(wallet, opid)
        assert_true(txid is not None, "the send should have produced a transaction")
        node.generate(1)
        wait_for_tx_scanned(wallet, txid)
        return txid

    def run_test(self) -> None:
        node = self.nodes[0]
        wallet = self.wallets[0]
        taddr = self.miner_addresses[0]

        # Account-mutating RPCs need a chain height to anchor a birthday to, so
        # the wallet must have committed a block before they are called.
        self.sync_all()
        sender = wallet.z_listaccounts()[0]['account_uuid']

        # Recipients live in a second account, so that a payment is a real
        # transfer rather than an account paying itself.
        recipient = wallet.z_getnewaccount("ironwood-privacy-recipient")['account_uuid']

        # Addresses carrying ONLY an Orchard receiver. Before NU6.3 they are paid
        # in Orchard; from NU6.3 the very same addresses are paid in Ironwood.
        recipient_orchard_ua = wallet.z_getaddressforaccount(
            recipient, [Receiver.ORCHARD])['address']
        sender_orchard_ua = wallet.z_getaddressforaccount(
            sender, [Receiver.ORCHARD])['address']
        sender_sapling_ua = wallet.z_getaddressforaccount(
            sender, [Receiver.SAPLING])['address']

        print("Mining to mature coinbase...")
        # Mined here rather than through `shield_coinbase`, which mines its own
        # maturity window: the Orchard era has to still be open after the mining
        # is done, so the block count has to stay under this test's control.
        node.generate(COINBASE_MATURITY + SPARE_BLOCKS)
        self.sync_all()
        # A wallet that has never reached the tip refuses every balance and spend
        # RPC, and reports its whole height as the distance outstanding.
        wait_for_wallet_sync(node, wallet)
        wait_for_balance_reads(wallet)

        print("Orchard era: shielding coinbase into Sapling to mint from...")
        wait_for_mature_coinbase_count(wallet, node.getblockcount() - COINBASE_MATURITY + 1)
        result = wallet.z_shieldcoinbase(
            taddr, sender_sapling_ua, INTERNAL_FEE, SHIELD_UTXOS)
        assert_equal(result['shieldingUTXOs'], SHIELD_UTXOS)
        txid = wait_and_assert_operationid_status(wallet, result['opid'])
        assert_true(txid is not None, "shielding should have produced a transaction")
        node.generate(1)
        wait_for_tx_scanned(wallet, txid)

        sapling_zat = wait_for_account_spendable(wallet, sender, Pool.SAPLING, min_zat=1)
        assert_true(
            sapling_zat > zat(ORCHARD_FUNDING + IRONWOOD_FUNDING),
            "the Sapling shield must cover both notes; got {} zat".format(sapling_zat))
        assert_true(
            node.getblockcount() < IRONWOOD_HEIGHT,
            "the Orchard note must be minted before NU6.3; raise IRONWOOD_HEIGHT")

        print("Orchard era: minting a {} ZEC Orchard note...".format(ORCHARD_FUNDING))
        # Sapling into an Orchard receiver crosses pools, so it needs
        # AllowRevealedAmounts. Pre-NU6.3 this mints a real Orchard note.
        self.send(node, wallet, sender_sapling_ua, sender_orchard_ua,
                  ORCHARD_FUNDING, PrivacyPolicy.ALLOW_REVEALED_AMOUNTS)
        wait_for_account_spendable(wallet, sender, Pool.ORCHARD,
                                   min_zat=zat(ORCHARD_FUNDING))
        assert_true(
            node.getblockcount() < IRONWOOD_HEIGHT,
            "the Orchard note must have been minted before NU6.3")

        print("Crossing into the Ironwood era...")
        target = IRONWOOD_HEIGHT + BLOCKS_PAST_ACTIVATION
        if node.getblockcount() < target:
            node.generate(target - node.getblockcount())
        # The wallet must see the new tip before it builds a spend, or it signs
        # with the pre-NU6.3 consensus branch id and the node rejects it.
        wait_for_wallet_sync(node, wallet)
        wait_account_settled(wallet, sender)
        assert_true(node.getblockcount() >= IRONWOOD_HEIGHT, "NU6.3 must be active")
        print("  NU6.3 active at height {}".format(node.getblockcount()))

        print("Ironwood era: minting a {} ZEC Ironwood note at the SAME Orchard "
              "receiver...".format(IRONWOOD_FUNDING))
        self.send(node, wallet, sender_sapling_ua, sender_orchard_ua,
                  IRONWOOD_FUNDING, PrivacyPolicy.ALLOW_REVEALED_AMOUNTS)
        wait_for_account_spendable(wallet, sender, Pool.IRONWOOD,
                                   min_zat=zat(IRONWOOD_FUNDING))
        wait_account_settled(wallet, sender)

        orchard, ironwood = self.pool_balances(wallet, sender)
        print("  sender holds {} zat in Orchard and {} zat in Ironwood"
              .format(orchard, ironwood))
        assert_true(orchard > 0, "the Orchard pool must hold pre-NU6.3 funds")
        assert_true(ironwood > 0, "the Ironwood pool must hold post-NU6.3 funds")

        # Above Ironwood alone, within Orchard's reach: the only way to pay this
        # is to spend Orchard funds across the turnstile, revealing the amount.
        needs_crossing = ironwood + zat(MARGIN)
        assert_true(
            needs_crossing <= orchard,
            "the payment must be one the Orchard pool could have covered "
            "({} zat needed, {} zat held)".format(needs_crossing, orchard))

        crossing_payment = [{'address': recipient_orchard_ua,
                             'amount': zec(needs_crossing)}]

        print("Phase 1: Orchard funds cannot pay an Orchard receiver under FullPrivacy...")
        # The discriminating case. Both under the default policy and under
        # FullPrivacy named explicitly: the default IS FullPrivacy, and a caller
        # relying on that must get the same protection as one that spells it out.
        for policy in [None, PrivacyPolicy.FULL_PRIVACY]:
            e = expect_rpc_error(
                wallet.z_sendmany,
                sender_orchard_ua,
                crossing_payment,
                MIN_CONFIRMATIONS,
                INTERNAL_FEE,
                policy,
            )
            assert_in_message(e, UNIFIED_RECEIVER)
            # The refusal has to say what would let the send through, or the
            # caller has nothing to act on.
            assert_in_message(e, WEAKEN_TO_REVEALED_AMOUNTS)
            # And it must come from the pre-flight, not from the built proposal:
            # this is the assertion that fails if Orchard and Ironwood are summed.
            assert_not_in_message(e, CROSSED_INTO_IRONWOOD)
        print("  refused before input selection, naming the receiver conflict")

        print("Phase 2: pczt_create applies the same check...")
        # A policy enforced on one and not the other would let a caller build the
        # very transaction it had just been refused.
        e = expect_rpc_error(
            wallet.pczt_create,
            sender_orchard_ua,
            crossing_payment,
            MIN_CONFIRMATIONS,
            PrivacyPolicy.FULL_PRIVACY,
        )
        assert_in_message(e, UNIFIED_RECEIVER)
        assert_not_in_message(e, CROSSED_INTO_IRONWOOD)

        print("Phase 3: Ironwood funds DO pay an Orchard receiver under FullPrivacy...")
        # The anti-regression direction: a payment the Ironwood pool covers on
        # its own crosses nothing and must still go through. It runs before the
        # crossing send below, which draws on Orchard and changes the balances.
        before_orchard, before_ironwood = self.pool_balances(wallet, recipient)
        self.send(node, wallet, sender_orchard_ua, recipient_orchard_ua,
                  MARGIN, PrivacyPolicy.FULL_PRIVACY)

        wait_for_account_spendable(
            wallet, recipient, Pool.IRONWOOD,
            min_zat=before_ironwood + zat(MARGIN))
        after_orchard, after_ironwood = self.pool_balances(wallet, recipient)
        assert_equal(after_ironwood, before_ironwood + zat(MARGIN))
        # The payment landed in Ironwood, not Orchard: past NU6.3 the turnstile
        # forbids value entering the Orchard pool at all.
        assert_equal(after_orchard, before_orchard)
        print("  payment arrived in the recipient's Ironwood pool")

        print("Phase 4: the refused send is permitted once revealed amounts are allowed...")
        # Nothing has changed except what the caller said it would accept, which
        # is what shows the refusals above were about the policy and not about the
        # funds. This is also the real migration path for an account whose
        # shielded funds are stranded in the legacy Orchard pool. The payment is
        # re-sized against the balances as they stand now, since phase 3 spent
        # from Ironwood.
        wait_account_settled(wallet, sender)
        orchard, ironwood = self.pool_balances(wallet, sender)
        needs_crossing = ironwood + zat(MARGIN)
        assert_true(
            needs_crossing <= orchard,
            "the crossing payment must still be within the Orchard pool's reach "
            "({} zat needed, {} zat held)".format(needs_crossing, orchard))

        self.send(node, wallet, sender_orchard_ua, recipient_orchard_ua,
                  zec(needs_crossing), PrivacyPolicy.ALLOW_REVEALED_AMOUNTS)
        print("  Orchard-to-Ironwood crossing accepted under AllowRevealedAmounts. PASSED")


if __name__ == '__main__':
    WalletZSendmanyPrivacyIronwoodTest().main()
