#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

from decimal import Decimal

from test_framework.test_framework import BitcoinTestFramework
from test_framework.authproxy import JSONRPCException
from test_framework.util import (
    COIN,
    COINBASE_MATURITY,
    Pool,
    account_balance_zat,
    assert_equal,
    assert_true,
    wait_and_assert_operationid_status,
    wait_for_account_balance,
    wait_for_account_spendable,
    wait_for_mature_coinbase_count,
    wait_for_tx_scanned,
    wait_for_wallet_sync,
)

# Test vector: Sapling extended spending key derived from seed [0; 32] for regtest.
REGTEST_SAPLING_KEY = "secret-extended-key-regtest1qqqqqqqqqqqqqq8n3zjjmvhhr854uy3qhpda3ml34haf0x388z5r7h4st4kpsf6qysqws3xh6qmha7gna72fs2n4clnc9zgyd22s658f65pex4exe56qjk5pqj9vfdq7dfdhjc2rs9jdwq0zl99uwycyrxzp86705rk687spn44e2uhm7h0hsagfvkk4n7n6nfer6u57v9cac84t7nl2zth0xpyfeg0w2p2wv2yn6jn923aaz0vdaml07l60ahapk6efchyxwysrvjsu94cs0"
REGTEST_SAPLING_ADDR = "zregtestsapling180m058urhazk8j98zvz9fsq5zd0vd9dpsc8c6ednwd2xkc3l8z9thmxsezepzx4aascp6acpzje"

# Amount funded to REGTEST_SAPLING_ADDR before the rescan-on-import scenarios.
FUND_AMOUNT = Decimal('1')
FUND_AMOUNT_ZAT = int(FUND_AMOUNT * COIN)

# Blocks mined on top of the funding tx to give the "missed by a late birthday"
# case a clear gap, while keeping the funding height inside the wallet's
# rewindable window. z_importkey's rescan="yes" rewinds via `truncate_to_height`,
# which refuses to rewind below the pruning floor at `tip - COINBASE_MATURITY`;
# the funded note (and the rewind target) must therefore stay within
# COINBASE_MATURITY blocks of the tip, so this buffer must be well under 100.
FUNDING_DEPTH = 20

# Blocks mined to bury the funding note below the finalized boundary (tip -
# COINBASE_MATURITY) between the rewind scenario and the fresh-import scenario.
# The rewind (truncate_to_height) can only reach the last COINBASE_MATURITY
# blocks, while the fresh-import history-recovery scans the finalized region; the
# two scenarios therefore need the note at different depths, so we run the rewind
# first (note in the window) and then bury the note before the fresh import.
BURY_DEPTH = 100

# Importing a key triggers a background "history recovery" re-scan that
# re-enhances transactions across the finalized region; on a chain this size
# that can take a few minutes, and the wallet tip dips until it catches back up.
# Give the scan-dependent waits plenty of headroom over the 60s/120s defaults.
SCAN_TIMEOUT = 450


class WalletImportExportKeyTest(BitcoinTestFramework):

    def __init__(self):
        super().__init__()
        self.cache_behavior = 'clean'
        # Two wallets, each on its own zebrad node (num_wallets == num_nodes).
        # Wallet 0 is the funder and target of the "found immediately" cases;
        # wallet 1 is a bystander used for isolation and the rewind scenario. We
        # deliberately keep the node count low: a larger mesh regularly fails to
        # converge at setup because a freshly-added peer link is not yet
        # gossip-ready when the first block is mined (zebra #10329).
        self.num_nodes = 2
        self.num_wallets = 2

    def run_test(self):
        node0 = self.nodes[0]
        node1 = self.nodes[1]
        w0 = self.wallets[0]
        w1 = self.wallets[1]

        # ---- Phase A: fund the external Sapling address --------------------
        # Neither wallet holds REGTEST_SAPLING_KEY yet, so the funded note is
        # opaque to both until the key is imported. The funding height is kept
        # within the rewindable window (last COINBASE_MATURITY blocks) so the
        # rewind scenario below can reach it.
        funding_height = self.fund_external_sapling_address()
        self.sync_all()
        wait_for_wallet_sync(node0, w0, timeout=SCAN_TIMEOUT)
        wait_for_wallet_sync(node1, w1, timeout=SCAN_TIMEOUT)

        # ---- Phase B: rescan="yes" rewinds to find a missed note -----------
        # Scenario (zallet #578 / #581): import the key into wallet 1 with a
        # birthday ABOVE the funding height, so the note is missed; then re-import
        # the same key with rescan="yes" and a birthday that covers the funding
        # height. The rewind truncates the scan queue back to just before that
        # height and re-scans, surfacing the previously-missed note. This runs
        # while the note is still inside the rewindable window.
        before1 = {a['account_uuid'] for a in w1.z_listaccounts()}
        result1 = w1.z_importkey(REGTEST_SAPLING_KEY, "whenkeyisnew", funding_height + 1)
        assert_equal(result1['address_type'], 'sapling')
        assert_equal(result1['address'], REGTEST_SAPLING_ADDR)
        imported1 = self.new_account(w1, before1)
        # The note sits below the import birthday, so the imported account starts
        # empty. (We cannot wait_for_wallet_sync here: after an import zallet's
        # wallet tip only tracks the finalized region until the chain advances,
        # so it never re-reports the full node tip on a static chain.)
        assert_equal(
            account_balance_zat(w1, imported1, Pool.SAPLING), 0,
            "a note funded before the import birthday must NOT be seen yet")

        # Isolation: wallet 1 holds the key now, wallet 0 does not.
        self.assert_cannot_export_key(w0)

        # Re-import the SAME key with rescan="yes", rewinding to the funding
        # height (which must stay within the pruning window, tip -
        # COINBASE_MATURITY, so rewind there rather than to genesis).
        before_rewind = {a['account_uuid'] for a in w1.z_listaccounts()}
        w1.z_importkey(REGTEST_SAPLING_KEY, "yes", funding_height)
        assert_equal({a['account_uuid'] for a in w1.z_listaccounts()}, before_rewind,
                     "re-importing a known key must not create a new account")
        # Nudge the sync engine so the re-queued range is scanned promptly. We do
        # not sync_all here: after an import the wallet tip only tracks the
        # finalized region, so a full-tip barrier can hang; wait_for_account_balance
        # polls the balance directly and tolerates the lagging tip.
        node0.generate(1)
        assert_equal(
            wait_for_account_balance(w1, imported1, Pool.SAPLING, FUND_AMOUNT_ZAT,
                                     timeout=SCAN_TIMEOUT),
            FUND_AMOUNT_ZAT,
            "rescan='yes' should rewind and surface the previously-missed note")
        assert_equal(w1.z_exportkey(REGTEST_SAPLING_ADDR), REGTEST_SAPLING_KEY)

        # ---- Phase C: bury the funding note below the finalized boundary ---
        # The fresh-import scenario below relies on the history-recovery scan,
        # which services the finalized region (tip - COINBASE_MATURITY); mine
        # enough blocks that the funding height drops out of the rewindable
        # window and into that region. We wait only wallet 0 to the new tip
        # (wallet 1 has imported the key, so its tip tracks only the finalized
        # region and a full sync_all would hang).
        node0.generate(BURY_DEPTH)
        wait_for_wallet_sync(node0, w0, timeout=SCAN_TIMEOUT)

        # ---- Phase D: fresh import with an early birthday finds the note ----
        # First, the input-validation cases, while wallet 0 still holds no key
        # (so start_height validation takes the new-key path).

        # start_height above the chain tip should fail.
        try:
            w0.z_importkey(REGTEST_SAPLING_KEY, "yes", 999999)
            assert_true(False, "Should have raised an exception")
        except JSONRPCException as e:
            assert_true("Block height out of range" in e.error['message'])

        # Invalid rescan value should fail.
        try:
            w0.z_importkey(REGTEST_SAPLING_KEY, "invalid_rescan")
            assert_true(False, "Should have raised an exception")
        except JSONRPCException as e:
            assert_true("Invalid rescan value" in e.error['message'])

        # Invalid key should fail.
        try:
            w0.z_importkey("not-a-valid-spending-key")
            assert_true(False, "Should have raised an exception")
        except JSONRPCException as e:
            assert_true("Invalid spending key" in e.error['message'])

        # Export for an address not in the wallet should fail.
        try:
            w0.z_exportkey("zregtestsapling1qqqqqqqqqqqqqqqqqqcguyvaw2vjk4sdyeg0lc970u659lvhqq7t0np6hlup5lusxle7505hlz3")
            assert_true(False, "Should have raised an exception")
        except JSONRPCException as e:
            assert_true("does not hold the spending key" in e.error['message'])

        # Scenario (zallet #579): wallet 0 mined past the funding height while
        # holding no key for REGTEST_SAPLING_ADDR. Importing with an early
        # birthday must reload the keys and surface the now-historical note.
        before0 = {a['account_uuid'] for a in w0.z_listaccounts()}
        result0 = w0.z_importkey(REGTEST_SAPLING_KEY, "whenkeyisnew", 1)
        assert_equal(result0['address_type'], 'sapling')
        assert_equal(result0['address'], REGTEST_SAPLING_ADDR)
        imported0 = self.new_account(w0, before0)
        assert_equal(
            wait_for_account_balance(w0, imported0, Pool.SAPLING, FUND_AMOUNT_ZAT,
                                     timeout=SCAN_TIMEOUT),
            FUND_AMOUNT_ZAT,
            "a fresh import with an early birthday should find the historical note")

        # The imported key appears in listaddresses as a unified address with a
        # sapling receiver under the "imported_watchonly" source.
        assert_true(self.count_imported_sapling_uas(w0) == 1,
                    "Imported key should appear as exactly one imported_watchonly "
                    "unified address with a sapling receiver")

        # Importing the same key again succeeds and returns the same address,
        # without creating a duplicate account.
        before_reimport = {a['account_uuid'] for a in w0.z_listaccounts()}
        result0b = w0.z_importkey(REGTEST_SAPLING_KEY)
        assert_equal(result0b['address'], REGTEST_SAPLING_ADDR)
        assert_equal(result0b['address_type'], 'sapling')
        assert_equal({a['account_uuid'] for a in w0.z_listaccounts()}, before_reimport,
                     "re-importing a known key must not create a new account")
        assert_equal(self.count_imported_sapling_uas(w0), 1)

        # Export the key back from wallet 0.
        assert_equal(w0.z_exportkey(REGTEST_SAPLING_ADDR), REGTEST_SAPLING_KEY)

        # ---- Phase E: spending from an imported key ------------------------
        self.assert_spend_from_imported_key_fails(w0)

    # -- helpers ------------------------------------------------------------

    def new_account(self, wallet, before_uuids):
        """Return the single account_uuid added since `before_uuids`.

        z_importkey returns only {address, address_type}, so the imported
        account is identified by diffing z_listaccounts() around the import.
        """
        new = {a['account_uuid'] for a in wallet.z_listaccounts()} - before_uuids
        assert_equal(len(new), 1,
                     "import should create exactly one new account, got %s" % new)
        return new.pop()

    def count_imported_sapling_uas(self, wallet):
        """Count imported_watchonly unified addresses with a sapling receiver."""
        count = 0
        for source in wallet.listaddresses():
            if source.get('source') != 'imported_watchonly':
                continue
            for ua_group in source.get('unified', []):
                for ua in ua_group.get('addresses', []):
                    if 'sapling' in ua.get('receiver_types', []):
                        count += 1
        return count

    def assert_cannot_export_key(self, wallet):
        """Assert `wallet` does not hold REGTEST_SAPLING_KEY's spending key."""
        try:
            wallet.z_exportkey(REGTEST_SAPLING_ADDR)
            assert_true(False, "Should have raised an exception")
        except JSONRPCException as e:
            assert_true("does not hold the spending key" in e.error['message'])

    def fund_external_sapling_address(self):
        """Fund REGTEST_SAPLING_ADDR with FUND_AMOUNT and return its block height.

        Wallet 0 (the "funder") shields its own mined coinbase, then sends
        FUND_AMOUNT from that Sapling balance to the external REGTEST_SAPLING_ADDR
        whose spending key no wallet holds yet. Extra blocks are mined afterwards
        so the funding height sits DETERMINISM_BUFFER below the tip.
        """
        funder = self.wallets[0]
        fnode = self.nodes[0]
        ftaddr = self.miner_addresses[0]

        self.sync_all()
        # Account 0 pre-exists and is seed-derived (i.e. spendable); remember it
        # so assert_spend_from_imported_key_fails can mint a valid destination
        # after the imported (non-spendable) account is added to this wallet.
        facct = funder.z_listaccounts()[0]['account_uuid']
        self.funder_acct = facct
        fua = funder.z_getaddressforaccount(facct, ['sapling'])['address']

        # Mine coinbase to maturity so the funder has something to shield.
        fnode.generate(COINBASE_MATURITY + 20)
        wait_for_mature_coinbase_count(
            funder, fnode.getblockcount() - COINBASE_MATURITY + 1)

        # Shield coinbase into the funder's own Sapling pool.
        shield = funder.z_shieldcoinbase(ftaddr, fua)
        shield_txid = wait_and_assert_operationid_status(funder, shield['opid'])
        assert_true(shield_txid is not None, "coinbase shield should have succeeded")
        fnode.generate(1)
        wait_for_tx_scanned(funder, shield_txid)
        wait_for_account_spendable(
            funder, facct, Pool.SAPLING, min_zat=FUND_AMOUNT_ZAT + 2 * COIN)

        # Send FUND_AMOUNT from the funder's Sapling to the external address.
        # fee is None so zallet computes the ZIP-317 fee itself.
        recipients = [{'address': REGTEST_SAPLING_ADDR, 'amount': FUND_AMOUNT}]
        opid = funder.z_sendmany(fua, recipients, 1, None)
        fund_txid = wait_and_assert_operationid_status(funder, opid)
        assert_true(fund_txid is not None, "funding send should have succeeded")
        fnode.generate(1)
        wait_for_tx_scanned(funder, fund_txid)
        funding_height = fnode.getblockcount()

        # Mine a small gap on top of the funding tx: enough that a later import
        # birthday can sit above it (so the note is missed), but shallow enough
        # that the funding height stays inside the rewindable window.
        fnode.generate(FUNDING_DEPTH)
        self.sync_all()
        return funding_height

    def assert_spend_from_imported_key_fails(self, wallet):
        """Spending from an imported key currently fails (suspected zallet bug).

        The imported account is stored as AccountPurpose::Spending
        { derivation: None }, and the spend path only rebuilds authority from
        seed + ZIP-32 derivation, so z_sendmany bails before signing. This is
        NOT part of #578/#581 (rescan-on-import); it is a separate bug tracked as
        zcash/zallet#582, where an imported *spending* key's funds cannot be
        spent. Asserted as a regression bracket: it should fail now, and once
        fixed this assertion flips. If it unexpectedly succeeds, fail loudly so
        we notice.
        """
        # Mint the destination on the wallet's own (seed-derived) account, not
        # the imported one, so the send can only fail on the *source* address.
        dest = wallet.z_getaddressforaccount(self.funder_acct, ['sapling'])['address']
        recipients = [{'address': dest, 'amount': Decimal('0.0001')}]
        try:
            opid = wallet.z_sendmany(REGTEST_SAPLING_ADDR, recipients, 1, None)
        except JSONRPCException as e:
            assert_true(
                "Invalid from address, no payment source found for address."
                in e.error['message'],
                "unexpected error spending from imported key: %s"
                % e.error['message'])
            return

        # No exception: the send was accepted. The suspected bug may be fixed —
        # surface that loudly so the regression bracket gets updated.
        assert_true(
            False,
            "z_sendmany from an imported key unexpectedly succeeded (opid=%s); "
            "the imported-key spend bug appears fixed — update this test." % opid)


if __name__ == '__main__':
    WalletImportExportKeyTest().main()
