# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

"""Shared scaffolding for the note-locking scenario suite (the
wallet_note_locking_*.py tests), against the Z3 stack (zebrad + zaino +
zallet).

Note locking is zallet's defense against the audit finding that two
overlapping spend operations on one account could select the same inputs
during the long selection-to-broadcast window and double-spend: when a spend
operation (z_sendmany, z_shieldcoinbase) creates its proposal, the selected
notes/UTXOs are LOCKED until the operation stores its transactions for
broadcast, fails (explicit unlock), or the chain advances past the lock
expiry height (self-healing after a crash; `builder.note_lock_blocks`
config, default 40 blocks).

The pieces every scenario shares live here: a test base class that lets a
scenario pick its lock window, an async-send helper (the lock is taken
synchronously inside the z_sendmany RPC call, so the caller observes the
locked state while the operation builds/proves in the background), a
crash-and-restart helper (SIGKILL, so no unlock code runs and the locks
survive in SQLite), and the conflict-error matcher.
"""

import time

from test_framework.config import ZalletArgs
from test_framework.util import (
    INTERNAL_FEE,
    MIN_CONFIRMATIONS,
    Pool,
    RpcProxy,
    account_locked_zat,
    account_spendable_zat,
    assert_true,
    kill_wallets,
    start_wallets,
    wait_for_wallet_sync,
)
from test_framework.util_ironwood import IronwoodTestFramework

# Zallet's default `builder.note_lock_blocks`: the number of blocks a spend
# proposal's input locks last. Mirrored here so the expiry scenario knows how
# far past the lock window to mine. Keep in sync with
# `zallet-core/src/config.rs` (DEFAULT_NOTE_LOCK_BLOCKS).
DEFAULT_NOTE_LOCK_BLOCKS = 40

# Substrings identifying the two acceptable shapes of a lock-conflict failure
# on a second, overlapping spend. "Account is busy" is the retryable error
# zallet maps a direct lock conflict (ProposalError::InputsLocked) to; an
# insufficient-funds proposal error arises when selection simply cannot see
# the locked inputs (selection excludes locked notes, so with every note
# locked there is nothing left to select). Both mean "the account's inputs
# are held by another in-flight operation; retry later", and which one a
# caller gets depends only on interleaving.
CONFLICT_NEEDLES = ('busy', 'nsufficient')


class NoteLockingScenario(IronwoodTestFramework):
    """A single-node, single-wallet regtest with every network upgrade
    (through NU6.3 / Ironwood) active at height 1, with zallet's note-lock
    window configurable per scenario.

    Subclasses set `NOTE_LOCK_BLOCKS` (None keeps zallet's default of 40
    blocks) and implement `run_test`. Funding follows the Ironwood suite's
    pattern: shielding mature coinbase into an Orchard receiver mints
    Ironwood notes, which the locking scenarios then spend.
    """

    # The lock window the scenario wants; None = zallet's default (40).
    #
    # The window is NOT part of the initial startup configuration: a zallet
    # that predates note locking rejects the unknown `note_lock_blocks`
    # config field at startup, which would kill the run during network setup,
    # before `skip_if_locking_absent` could turn it into a skip. Scenarios
    # therefore start against zallet's stock configuration, probe for
    # support, and then call `apply_lock_window` to restart with the window
    # applied (a no-op when it is None).
    NOTE_LOCK_BLOCKS = None

    # Whether `apply_lock_window` has run; once set, every later wallet
    # (re)start (including the crash-and-restart helpers) carries the
    # scenario's window, so a restart never silently reverts to the default.
    _lock_window_applied = False

    def setup_wallets(self) -> list[RpcProxy]:
        lock_window = (
            self.NOTE_LOCK_BLOCKS if self._lock_window_applied else None)
        zallet_args = [
            ZalletArgs(activation_heights=self.activation_heights,
                       note_lock_blocks=lock_window)
            for _ in range(self.num_wallets)
        ]
        return start_wallets(
            self.num_wallets, self.options.tmpdir, zallet_args=zallet_args)

    def apply_lock_window(self) -> RpcProxy:
        """Restart zallet with the scenario's `NOTE_LOCK_BLOCKS` in effect,
        returning the new wallet handle. Call only after
        `skip_if_locking_absent` has confirmed the binary supports note
        locking; a no-op when the scenario keeps zallet's default window.

        The restart uses the crash path (SIGKILL): zallet has no graceful
        stop, and at this point in a scenario nothing is in flight, so the
        persisted wallet state is simply reopened by the new process."""
        if self.NOTE_LOCK_BLOCKS is None:
            return self.wallets[0]
        self._lock_window_applied = True
        self.crash_zallet()
        return self.restart_zallet()

    def skip_if_locking_absent(self, wallet: RpcProxy) -> bool:
        """True (after printing a skip notice) when the zallet under test
        predates the note-locking surface, so the suite degrades to a skip
        rather than a spurious failure against an old binary."""
        if 'z_clearlockedoutputs' in wallet.help():
            return False
        print("SKIP: this zallet has no note-locking surface "
              "(z_clearlockedoutputs is absent); skipping.")
        return True

    def send_async(self, wallet: RpcProxy, from_ua: str, to_ua: str,
                   amount) -> str:
        """Start a z_sendmany WITHOUT waiting for the operation: returns the
        operation id as soon as the RPC returns. The proposal (and therefore
        the input locking) happens synchronously inside the RPC call, so by
        the time this returns the selected inputs are locked and the caller
        can observe the locked state while the operation builds and proves in
        the background."""
        return wallet.z_sendmany(
            from_ua, [{'address': to_ua, 'amount': amount}],
            MIN_CONFIRMATIONS, INTERNAL_FEE)

    def crash_zallet(self) -> None:
        """Simulate a wallet crash: SIGKILL zallet (no graceful stop RPC), so
        no unlock or cleanup code runs and any note locks held by in-flight
        operations stay recorded in the wallet database."""
        kill_wallets(self.wallets)

    def restart_zallet(self) -> RpcProxy:
        """Start zallet again on the persisted wallet DB (after `crash_zallet`
        or a graceful stop) and block until it resyncs to the node tip.
        Returns the new wallet RPC handle."""
        self.wallets = self.setup_wallets()
        wait_for_wallet_sync(self.nodes[0], self.wallets[0])
        return self.wallets[0]

    def wait_for_unlocked_spendable(self, wallet: RpcProxy, account: str,
                                    pool: Pool, min_zat: int,
                                    timeout: int = 120) -> int:
        """Block until the account holds no locked value in `pool` AND its
        spendable balance there is at least `min_zat`, then return the
        spendable balance. This is the settle point after a lock has been
        released (store/unlock/clear) or has expired."""
        deadline = time.time() + timeout
        locked = spendable = None
        while time.time() < deadline:
            try:
                locked = account_locked_zat(wallet, account, pool)
                spendable = account_spendable_zat(wallet, account, pool)
                if locked == 0 and spendable >= min_zat:
                    return spendable
            except Exception:
                pass
            time.sleep(1)
        raise AssertionError(
            "wait_for_unlocked_spendable: timeout after {}s; locked={} "
            "spendable={} (wanted locked=0, spendable>={})".format(
                timeout, locked, spendable, min_zat))


def assert_conflict_error(e) -> None:
    """Assert that a JSONRPCException is one of the two acceptable shapes of
    a lock-conflict failure (see CONFLICT_NEEDLES)."""
    msg = e.error['message']
    assert_true(
        any(needle in msg for needle in CONFLICT_NEEDLES),
        "expected a retryable lock-conflict error (one of {!r}), got: {!r}"
        .format(CONFLICT_NEEDLES, msg))
