#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Exercises `zallet backup` and a restore (zcash/zallet#195, #49).
#
# Funds a wallet with mature transparent coinbase, takes an online backup of
# the running wallet, then a second backup of the stopped wallet, erases the
# live database, and restores from the online backup, checking the wallet
# comes back with the same account and unspent outputs. Also checks that the
# backup is written owner-only and with an encryption-identity reminder, that
# backup never overwrites an existing file, and that it refuses a datadir with
# no wallet database.
#

import os
import shutil
import subprocess
import time

from test_framework.test_framework import BitcoinTestFramework
from test_framework.config import ZalletArgs
from test_framework.util import (
    COINBASE_MATURITY,
    Pool,
    assert_equal,
    assert_true,
    nu_activation_all_at_1_with_ironwood,
    start_wallet,
    start_wallets,
    wallet_dir,
    zallet_binary,
    zallet_processes,
)


class WalletBackupTest(BitcoinTestFramework):

    def __init__(self):
        super().__init__()
        self.num_nodes = 1
        self.num_wallets = 1
        self.cache_behavior = 'clean'
        self.activation_heights = nu_activation_all_at_1_with_ironwood()

    def setup_wallets(self):
        zallet_args = [
            ZalletArgs(activation_heights=self.activation_heights)
            for _ in range(self.num_wallets)
        ]
        return start_wallets(
            self.num_wallets, self.options.tmpdir, zallet_args=zallet_args)

    def wallet_datadir(self):
        return wallet_dir(self.options.tmpdir, 0)

    def run_backup(self, destination, expect_success, datadir=None):
        # `zallet backup` is a CLI subcommand, not an RPC; invoke it the same
        # way the harness invokes init-wallet-encryption and generate-mnemonic.
        args = [zallet_binary(), "-d=" + (datadir or self.wallet_datadir()),
                "backup", destination]
        result = subprocess.run(args, capture_output=True, text=True)
        if expect_success:
            assert_equal(result.returncode, 0)
        else:
            assert_true(result.returncode != 0,
                        "backup should have failed but exited 0")
        return result

    def transparent_unspent(self, wallet):
        unspent = wallet.z_listunspent(0)
        txos = [u for u in unspent if u['pool'] == Pool.TRANSPARENT]
        total = sum(u['valueZat'] for u in txos)
        return txos, total

    def run_test(self):
        node = self.nodes[0]
        w = self.wallets[0]
        backups = os.path.join(self.options.tmpdir, "backups")
        os.makedirs(backups)

        # Fund the wallet with mature transparent coinbase.
        print("Mining mature coinbase to the wallet...")
        node.generate(COINBASE_MATURITY + 2)
        self.sync_all()

        acct = w.z_listaccounts()[0]['account_uuid']
        before_txos, before_total = self.transparent_unspent(w)
        assert_true(before_total > 0,
                    "wallet should hold mature transparent coinbase")
        print("Before backup: account {}, {} transparent output(s), total {}"
              .format(acct, len(before_txos), before_total))

        # An online backup must succeed against the running wallet: `backup`
        # takes a transaction-consistent snapshot via SQLite's Online Backup
        # API and no longer requires exclusive use of the datadir.
        print("Taking an online backup of the running wallet...")
        online = self.run_backup(backups, expect_success=True)
        assert_true("pages" in online.stdout,
                    "online backup should report a page count")
        assert_true(zallet_processes[0].poll() is None,
                    "the running wallet must survive an online backup")
        files = [f for f in os.listdir(backups)
                 if f.startswith("zallet-wallet-backup-") and f.endswith(".db")]
        assert_equal(len(files), 1)
        online_backup = os.path.join(backups, files[0])

        # The backup contains the same key material as the wallet database, so
        # it must be written owner-only.
        mode = os.stat(online_backup).st_mode & 0o777
        assert_equal(0o600, mode,
                     "backup must be readable only by its owner")

        # Restoring also needs the age encryption identity, which the backup
        # deliberately does not include; the command must say so.
        assert_true("encryption identity" in online.stdout,
                    "backup should remind about the encryption identity")

        # Generated backup names have one-second resolution; ensure the next
        # backup cannot collide with the online one.
        time.sleep(1.1)

        # Stop the wallet and back it up again: repeated backups into the same
        # directory rotate rather than collide.
        print("Stopping the wallet and backing it up again...")
        w.stop()
        zallet_processes[0].wait()

        result = self.run_backup(backups, expect_success=True)
        assert_true("pages" in result.stdout,
                    "backup should report a page count")
        files = [f for f in os.listdir(backups)
                 if f.startswith("zallet-wallet-backup-") and f.endswith(".db")]
        assert_equal(len(files), 2)

        # An explicit existing path is refused.
        self.run_backup(online_backup, expect_success=False)

        # A datadir with no wallet database has nothing to back up, and the
        # command must fail rather than write an empty or missing-source file.
        print("Backup must refuse a datadir with no wallet database...")
        empty_datadir = os.path.join(self.options.tmpdir, "empty-datadir")
        os.makedirs(empty_datadir)
        shutil.copyfile(os.path.join(self.wallet_datadir(), "zallet.toml"),
                        os.path.join(empty_datadir, "zallet.toml"))
        missing = self.run_backup(backups, expect_success=False,
                                  datadir=empty_datadir)
        assert_true("nothing to back up" in missing.stderr + missing.stdout,
                    "a datadir with no wallet should report nothing to back up")

        # Erase the live database, keeping the encryption identity in place,
        # and restore from the ONLINE backup: the snapshot taken while the
        # wallet was running must itself be restorable.
        print("Erasing the wallet database and restoring from the online backup...")
        datadir = self.wallet_datadir()
        live_db = os.path.join(datadir, "wallet.db")
        for suffix in ("", "-wal", "-shm"):
            path = live_db + suffix
            if os.path.exists(path):
                os.remove(path)
        shutil.copyfile(online_backup, live_db)

        # Restart the wallet; start_wallet skips key generation because the
        # restored database is already present.
        self.wallets[0] = start_wallet(
            0, self.options.tmpdir,
            zallet_args=ZalletArgs(activation_heights=self.activation_heights))
        w = self.wallets[0]

        restored_acct = w.z_listaccounts()[0]['account_uuid']
        after_txos, after_total = self.transparent_unspent(w)
        print("After restore: account {}, {} transparent output(s), total {}"
              .format(restored_acct, len(after_txos), after_total))

        assert_equal(restored_acct, acct)
        assert_equal(after_total, before_total)
        assert_equal(len(after_txos), len(before_txos))
        print("PASSED: online backup and restore preserved the account and funds")


if __name__ == '__main__':
    WalletBackupTest().main()
