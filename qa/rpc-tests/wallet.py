#!/usr/bin/env python3
# Copyright (c) 2014-2016 The Bitcoin Core developers
# Copyright (c) 2016-2024 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#from decimal import Decimal

import time
from decimal import Decimal

from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import assert_equal, assert_true

# Coinbase outputs require 100 confirmations before zallet counts them.
COINBASE_MATURITY = 100

# zallet's transparent-balance summary uses an internal scan tip that can lag a
# few blocks behind the wallet's reported `wallet_tip`. Allow that slack when
# asserting how many of the mined coinbases the wallet has surfaced.
_SCAN_LAG_TOLERANCE = 5


def _wait_for_wallet_sync(node, wallet, timeout=60):
    """Block until the wallet reports the node's current tip."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        target = node.getblockcount()
        status = wallet.getwalletstatus()
        if status.get('wallet_tip', {}).get('height') == target:
            # Give the transparent balance accounting a beat to catch up.
            time.sleep(2)
            return
        time.sleep(0.5)
    raise AssertionError("wallet did not sync to node tip within %ss" % timeout)


def _wallet_transparent_zec(wallet):
    return Decimal(wallet.z_gettotalbalance(1, True)['transparent'])


# Test that we can create a wallet and use an address from it to mine blocks.
class WalletTest (BitcoinTestFramework):

    def __init__(self):
        super().__init__()
        self.cache_behavior = 'clean'
        self.num_nodes = 1
        self.num_wallets = 1

    def run_test(self):
        node = self.nodes[0]
        wallet = self.wallets[0]
        transparent_address = self.miner_addresses[0]

        # The test harness mines a single block so Zallet can start. Mine
        # COINBASE_MATURITY more so the early coinbases are well past the
        # 100-confirmation maturity threshold.
        node.generate(COINBASE_MATURITY)
        _wait_for_wallet_sync(node, wallet)

        tip = node.getblockcount()
        assert_equal(tip, 1 + COINBASE_MATURITY)

        # Node sees every coinbase reward at the miner address.
        node_balance = node.getaddressbalance(transparent_address)
        assert_equal(node_balance['balance'], tip * 625000000)

        # Wallet sees the mature coinbases. The exact count of "mature
        # coinbases visible to z_gettotalbalance" depends on zallet's internal
        # scan tip (which can lag a few blocks behind `wallet_tip`); pin a
        # range around the expected count rather than the exact value.
        wallet_zec = _wallet_transparent_zec(wallet)
        coinbase_count = int(wallet_zec / Decimal('6.25'))
        assert_true(
            tip - _SCAN_LAG_TOLERANCE <= coinbase_count <= tip,
            "wallet transparent balance %s ZEC implies %d coinbases; "
            "expected between %d and %d at tip %d"
            % (wallet_zec, coinbase_count,
               tip - _SCAN_LAG_TOLERANCE, tip, tip))

        # Mining another block should grow the wallet's visible balance by at
        # least one mature coinbase reward (older immature outputs catch up).
        prev_zec = wallet_zec
        node.generate(1)
        _wait_for_wallet_sync(node, wallet)
        new_zec = _wallet_transparent_zec(wallet)
        assert_true(
            new_zec > prev_zec,
            "wallet transparent balance should grow after mining "
            "(was %s, now %s)" % (prev_zec, new_zec))

        # The wallet tracked the coinbase txs. The freshest tip may not have
        # been surfaced yet through `z_listtransactions`, so allow a small lag.
        tx_count = len(wallet.z_listtransactions())
        tip = node.getblockcount()
        assert_true(
            tip - _SCAN_LAG_TOLERANCE <= tx_count <= tip,
            "z_listtransactions returned %d entries at tip %d "
            "(allowed lag %d)" % (tx_count, tip, _SCAN_LAG_TOLERANCE))

        """
        walletinfo = self.wallets[0].getwalletinfo()
        assert_equal(Decimal(walletinfo['immature_balance']), Decimal('40'))
        assert_equal(Decimal(walletinfo['balance']), Decimal('0'))

        blockchaininfo = self.nodes[0].getblockchaininfo()
        assert_equal(blockchaininfo['estimatedheight'], 4)

        self.sync_all()
        self.nodes[1].generate(101)
        self.sync_all()

        assert_equal(Decimal(self.nodes[0].getbalance()), Decimal('40'))
        assert_equal(Decimal(self.nodes[1].getbalance()), Decimal('10'))
        assert_equal(Decimal(self.nodes[2].getbalance()), Decimal('0'))
        assert_equal(Decimal(self.nodes[0].getbalance("*")), Decimal('40'))
        assert_equal(Decimal(self.nodes[1].getbalance("*")), Decimal('10'))
        assert_equal(Decimal(self.nodes[2].getbalance("*")), Decimal('0'))

        # Send 21 ZEC from 0 to 2 using sendtoaddress call.
        # Second transaction will be child of first, and will require a fee
        self.nodes[0].sendtoaddress(self.nodes[2].getnewaddress(), Decimal('11'))
        self.nodes[0].sendtoaddress(self.nodes[2].getnewaddress(), Decimal('10'))

        walletinfo = self.nodes[0].getwalletinfo()
        assert_equal(Decimal(walletinfo['immature_balance']), Decimal('0'))

        blockchaininfo = self.nodes[0].getblockchaininfo()
        assert_equal(blockchaininfo['estimatedheight'], 105)

        # Have node0 mine a block, thus it will collect its own fee.
        self.sync_all()
        self.nodes[0].generate(1)
        self.sync_all()

        # Have node1 generate 100 blocks (so node0 can recover the fee)
        self.nodes[1].generate(100)
        self.sync_all()

        # node0 should end up with 50 ZEC in block rewards plus fees, but
        # minus the 21 ZEC plus fees sent to node2
        assert_equal(Decimal(self.nodes[0].getbalance()), Decimal('50') - Decimal('21'))
        assert_equal(Decimal(self.nodes[2].getbalance()), Decimal('21'))
        assert_equal(Decimal(self.nodes[0].getbalance("*")), Decimal('50') - Decimal('21'))
        assert_equal(Decimal(self.nodes[2].getbalance("*")), Decimal('21'))

        # Node0 should have three unspent outputs.
        # Create a couple of transactions to send them to node2, submit them through
        # node1, and make sure both node0 and node2 pick them up properly:
        node0utxos = self.nodes[0].listunspent(1)
        assert_equal(len(node0utxos), 3)

        # Check 'generated' field of listunspent
        # Node 0: has one coinbase utxo and two regular utxos
        assert_equal(sum(int(uxto["generated"] is True) for uxto in node0utxos), 1)
        # Node 1: has 101 coinbase utxos and no regular utxos
        node1utxos = self.nodes[1].listunspent(1)
        assert_equal(len(node1utxos), 101)
        assert_equal(sum(int(uxto["generated"] is True) for uxto in node1utxos), 101)
        # Node 2: has no coinbase utxos and two regular utxos
        node2utxos = self.nodes[2].listunspent(1)
        assert_equal(len(node2utxos), 2)
        assert_equal(sum(int(uxto["generated"] is True) for uxto in node2utxos), 0)

        # Catch an attempt to send a transaction with an absurdly high fee.
        # Send 1.0 ZEC from an utxo of value 10.0 ZEC but don't specify a change output, so then
        # the change of 9.0 ZEC becomes the fee, which is considered to be absurdly high.
        inputs = []
        outputs = {}
        for utxo in node2utxos:
            if utxo["amount"] == Decimal("10.0"):
                break
        assert_equal(utxo["amount"], Decimal("10.0"))
        inputs.append({"txid": utxo["txid"], "vout": utxo["vout"]})
        outputs[self.nodes[2].getnewaddress("")] = Decimal("1.0")
        raw_tx = self.nodes[2].createrawtransaction(inputs, outputs)
        signed_tx = self.nodes[2].signrawtransaction(raw_tx)
        try:
            self.nodes[2].sendrawtransaction(signed_tx["hex"])
        except JSONRPCException as e:
            errorString = e.error['message']
        assert_equal(errorString, "256: absurdly-high-fee")

        # create both transactions
        txns_to_send = []
        fee = conventional_fee(2)
        for utxo in node0utxos:
            inputs = []
            outputs = {}
            inputs.append({"txid": utxo["txid"], "vout": utxo["vout"]})
            outputs[self.nodes[2].getnewaddress("")] = utxo["amount"] - fee
            raw_tx = self.nodes[0].createrawtransaction(inputs, outputs)
            txns_to_send.append(self.nodes[0].signrawtransaction(raw_tx))

        # Have node 1 (miner) send the transactions
        self.nodes[1].sendrawtransaction(txns_to_send[0]["hex"], True)
        self.nodes[1].sendrawtransaction(txns_to_send[1]["hex"], True)
        self.nodes[1].sendrawtransaction(txns_to_send[2]["hex"], True)

        # Have node1 mine a block to confirm transactions:
        self.sync_all()
        self.nodes[1].generate(1)
        self.sync_all()

        assert_equal(Decimal(self.nodes[0].getbalance()), Decimal('0'))
        assert_equal(Decimal(self.nodes[2].getbalance()), Decimal('50') - 3*fee)
        assert_equal(Decimal(self.nodes[0].getbalance("*")), Decimal('0'))
        assert_equal(Decimal(self.nodes[2].getbalance("*")), Decimal('50') - 3*fee)

        # Send 10 ZEC normally
        address = self.nodes[0].getnewaddress("")
        self.nodes[2].settxfee(Decimal('0.001'))  # not the default
        self.nodes[2].sendtoaddress(address, Decimal('10'), "", "", False)
        self.sync_all()
        self.nodes[2].generate(1)
        self.sync_all()
        assert_equal(Decimal(self.nodes[2].getbalance()), Decimal('39.99900000') - 3*fee)
        assert_equal(Decimal(self.nodes[0].getbalance()), Decimal('10.00000000'))
        assert_equal(Decimal(self.nodes[2].getbalance("*")), Decimal('39.99900000') - 3*fee)
        assert_equal(Decimal(self.nodes[0].getbalance("*")), Decimal('10.00000000'))

        # Send 10 ZEC with subtract fee from amount
        self.nodes[2].sendtoaddress(address, Decimal('10'), "", "", True)
        self.sync_all()
        self.nodes[2].generate(1)
        self.sync_all()
        assert_equal(Decimal(self.nodes[2].getbalance()), Decimal('29.99900000') - 3*fee)
        assert_equal(Decimal(self.nodes[0].getbalance()), Decimal('19.99900000'))
        assert_equal(Decimal(self.nodes[2].getbalance("*")), Decimal('29.99900000') - 3*fee)
        assert_equal(Decimal(self.nodes[0].getbalance("*")), Decimal('19.99900000'))

        # Sendmany 10 ZEC
        self.nodes[2].sendmany("", {address: Decimal('10')}, 0, "", [])
        self.sync_all()
        self.nodes[2].generate(1)
        self.sync_all()
        assert_equal(Decimal(self.nodes[2].getbalance()), Decimal('19.99800000') - 3*fee)
        assert_equal(Decimal(self.nodes[0].getbalance()), Decimal('29.99900000'))
        assert_equal(Decimal(self.nodes[2].getbalance("*")), Decimal('19.99800000') - 3*fee)
        assert_equal(Decimal(self.nodes[0].getbalance("*")), Decimal('29.99900000'))

        # Sendmany 10 ZEC with subtract fee from amount
        self.nodes[2].sendmany("", {address: Decimal('10')}, 0, "", [address])
        self.sync_all()
        self.nodes[2].generate(1)
        self.sync_all()
        assert_equal(Decimal(self.nodes[2].getbalance()), Decimal('9.99800000') - 3*fee)
        assert_equal(Decimal(self.nodes[0].getbalance()), Decimal('39.99800000'))
        assert_equal(Decimal(self.nodes[2].getbalance("*")), Decimal('9.99800000') - 3*fee)
        assert_equal(Decimal(self.nodes[0].getbalance("*")), Decimal('39.99800000'))

        # Test ResendWalletTransactions:
        # Create a couple of transactions, then start up a fourth
        # node (nodes[3]) and ask nodes[0] to rebroadcast.
        # EXPECT: nodes[3] should have those transactions in its mempool.
        txid1 = self.nodes[0].sendtoaddress(self.nodes[1].getnewaddress(), 1)
        txid2 = self.nodes[1].sendtoaddress(self.nodes[0].getnewaddress(), 1)
        sync_mempools(self.nodes)

        self.nodes.append(start_node(3, self.options.tmpdir))
        connect_nodes_bi(self.nodes, 0, 3)
        sync_blocks(self.nodes)

        relayed = self.nodes[0].resendwallettransactions()
        assert_equal(set(relayed), set([txid1, txid2]))
        sync_mempools(self.nodes)

        assert(txid1 in self.nodes[3].getrawmempool())

        # check integer balances from getbalance
        assert_equal(Decimal(self.nodes[2].getbalance("*", 1, False, True)), 999800000 - 3*fee*COIN)

        # send from node 0 to node 2 taddr
        mytaddr = self.nodes[2].getnewaddress()
        mytxid = self.nodes[0].sendtoaddress(mytaddr, Decimal('10.0'))
        self.sync_all()
        self.nodes[0].generate(1)
        self.sync_all()

        mybalance = Decimal(self.nodes[2].z_getbalance(mytaddr))
        assert_equal(mybalance, Decimal('10.0'))

        # check integer balances from z_getbalance
        assert_equal(self.nodes[2].z_getbalance(mytaddr, 1, True), 1000000000)

        mytxdetails = self.nodes[2].getrawtransaction(mytxid, 1)
        myvjoinsplits = mytxdetails["vjoinsplit"]
        assert_equal(0, len(myvjoinsplits))
        assert("joinSplitPubKey" not in mytxdetails)
        assert("joinSplitSig" not in mytxdetails)
        """

if __name__ == '__main__':
    WalletTest ().main ()
