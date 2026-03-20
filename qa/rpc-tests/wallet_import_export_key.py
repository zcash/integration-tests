#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

from test_framework.test_framework import BitcoinTestFramework
from test_framework.authproxy import JSONRPCException
from test_framework.util import assert_equal, assert_true

# Test vector: Sapling extended spending key derived from seed [0; 32] for regtest.
REGTEST_SAPLING_KEY = "secret-extended-key-regtest1qqqqqqqqqqqqqq8n3zjjmvhhr854uy3qhpda3ml34haf0x388z5r7h4st4kpsf6qysqws3xh6qmha7gna72fs2n4clnc9zgyd22s658f65pex4exe56qjk5pqj9vfdq7dfdhjc2rs9jdwq0zl99uwycyrxzp86705rk687spn44e2uhm7h0hsagfvkk4n7n6nfer6u57v9cac84t7nl2zth0xpyfeg0w2p2wv2yn6jn923aaz0vdaml07l60ahapk6efchyxwysrvjsu94cs0"
REGTEST_SAPLING_ADDR = "zregtestsapling180m058urhazk8j98zvz9fsq5zd0vd9dpsc8c6ednwd2xkc3l8z9thmxsezepzx4aascp6acpzje"

class WalletImportExportKeyTest(BitcoinTestFramework):

    def __init__(self):
        super().__init__()
        self.cache_behavior = 'clean'
        self.num_nodes = 2
        self.num_wallets = 2

    def run_test(self):
        wallet0 = self.wallets[0]
        wallet1 = self.wallets[1]

        # Import the test key into wallet 0.
        result = wallet0.z_importkey(REGTEST_SAPLING_KEY)
        assert_equal(result['address_type'], 'sapling')
        assert_equal(result['address'], REGTEST_SAPLING_ADDR)

        # Verify the imported key appears in listaddresses as a unified address
        # with a sapling receiver under the "imported_watchonly" source.
        addresses = wallet0.listaddresses()
        found = False
        for source in addresses:
            if source.get('source') == 'imported_watchonly':
                for ua_group in source.get('unified', []):
                    for ua in ua_group.get('addresses', []):
                        if 'sapling' in ua.get('receiver_types', []):
                            found = True
        assert_true(found,
                    "Imported key should appear as a unified address with sapling receiver")

        # Importing the same key again should succeed and return the same address,
        # without creating a duplicate account.
        result2 = wallet0.z_importkey(REGTEST_SAPLING_KEY)
        assert_equal(result2['address'], result['address'])
        assert_equal(result2['address_type'], 'sapling')
        addresses_after = wallet0.listaddresses()
        sapling_ua_count = 0
        for source in addresses_after:
            if source.get('source') == 'imported_watchonly':
                for ua_group in source.get('unified', []):
                    for ua in ua_group.get('addresses', []):
                        if 'sapling' in ua.get('receiver_types', []):
                            sapling_ua_count += 1
        assert_equal(sapling_ua_count, 1)
        
        # Export the key back from wallet 0.
        exported_key = wallet0.z_exportkey(REGTEST_SAPLING_ADDR)
        assert_equal(exported_key, REGTEST_SAPLING_KEY)

        # Wallet isolation: wallet 1 should not be able to export a key
        # that was only imported into wallet 0.
        try:
            wallet1.z_exportkey(REGTEST_SAPLING_ADDR)
            assert_true(False, "Should have raised an exception")
        except JSONRPCException as e:
            assert_true("does not hold the spending key" in e.error['message'])

        # Import the same key into wallet 1.
        result3 = wallet1.z_importkey(REGTEST_SAPLING_KEY)
        assert_equal(result3['address_type'], 'sapling')
        assert_equal(result3['address'], REGTEST_SAPLING_ADDR)
        
        # Export from wallet 1 should also match.
        exported_key2 = wallet1.z_exportkey(REGTEST_SAPLING_ADDR)
        assert_equal(exported_key2, REGTEST_SAPLING_KEY)
        
        # Test rescan parameter values.
        result_yes = wallet0.z_importkey(REGTEST_SAPLING_KEY, "yes")
        assert_equal(result_yes['address'], REGTEST_SAPLING_ADDR)

        result_no = wallet0.z_importkey(REGTEST_SAPLING_KEY, "no")
        assert_equal(result_no['address'], REGTEST_SAPLING_ADDR)

        result_whenkeyisnew = wallet0.z_importkey(REGTEST_SAPLING_KEY, "whenkeyisnew")
        assert_equal(result_whenkeyisnew['address'], REGTEST_SAPLING_ADDR)

        # start_height above chain tip should fail.
        try:
            wallet0.z_importkey(REGTEST_SAPLING_KEY, "yes", 999999)
            assert_true(False, "Should have raised an exception")
        except JSONRPCException as e:
            assert_true("Block height out of range" in e.error['message'])

        # Invalid rescan value should fail.
        try:
            wallet0.z_importkey(REGTEST_SAPLING_KEY, "invalid_rescan")
            assert_true(False, "Should have raised an exception")
        except JSONRPCException as e:
            assert_true("Invalid rescan value" in e.error['message'])
            
        # Invalid key should fail.
        try:
            wallet0.z_importkey("not-a-valid-spending-key")
            assert_true(False, "Should have raised an exception")
        except JSONRPCException as e:
            assert_true("Invalid spending key" in e.error['message'])

        # Export for an address not in the wallet should fail.
        try:
            wallet0.z_exportkey("zregtestsapling1qqqqqqqqqqqqqqqqqqcguyvaw2vjk4sdyeg0lc970u659lvhqq7t0np6hlup5lusxle7505hlz3")
            assert_true(False, "Should have raised an exception")
        except JSONRPCException as e:
            assert_true("does not hold the spending key" in e.error['message'])

if __name__ == '__main__':
    WalletImportExportKeyTest().main()
