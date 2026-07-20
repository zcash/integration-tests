# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

"""Ironwood (NU6.3, ZIP 2005) specific test scaffolding for the Z3 stack
(zebrad + zaino + zallet).

The generic building blocks (shielding coinbase, self-sends, mature-coinbase
waits, per-pool view filters) live in `test_framework.util`; this module holds
only the Ironwood-specific pieces: the test base class that activates NU6.3 on
both zebrad and zallet, and thin wrappers that pin the pool to Ironwood.

There is no coinbase-to-Ironwood path: funds enter the Ironwood pool only by
shielding transparent coinbase into an Orchard receiver once NU6.3 is active
(Ironwood notes are Orchard-shaped, so an Orchard receiver post-NU6.3 mints
version-3 Orchard / Ironwood notes).
"""

from test_framework.test_framework import BitcoinTestFramework
from test_framework.config import ZalletArgs
from test_framework.util import (
    Pool,
    RpcProxy,
    nu_activation_all_at_1_with_ironwood,
    outputs_in_pool,
    shield_coinbase,
    spends_in_pool,
    start_wallets,
)


class IronwoodTestFramework(BitcoinTestFramework):
    """A single-node, single-wallet regtest with NU6.3 (Ironwood) active at
    height 1.

    The NU6.3 activation heights are applied to zebra (the base `setup_nodes`
    reads `self.activation_heights`) and baked into the wallet DB at creation
    time (the base `prepare_wallets` does likewise, which the Ironwood
    shard-scan-ranges migration depends on). The only override needed is
    `setup_wallets`, to START zallet with the same heights so its network
    params agree with zebra's coinbase branch id; otherwise zallet rejects the
    first block it syncs.

    Subclasses implement `run_test` and may raise `self.num_wallets` if they
    genuinely need a second wallet, though the framework provisions one zebrad
    per wallet and concurrent wallets expose a known Zaino race.
    """

    def __init__(self) -> None:
        super().__init__()
        self.num_nodes = 1
        self.num_wallets = 1
        self.cache_behavior = 'clean'
        self.activation_heights = nu_activation_all_at_1_with_ironwood()

    def setup_wallets(self) -> list[RpcProxy]:
        zallet_args = [
            ZalletArgs(activation_heights=self.activation_heights)
            for _ in range(self.num_wallets)
        ]
        return start_wallets(
            self.num_wallets, self.options.tmpdir, zallet_args=zallet_args)


def shield_coinbase_into_ironwood(node: RpcProxy, wallet: RpcProxy, taddr: str,
                                  ua: str, acct: str,
                                  extra: int = 20) -> tuple[str, int]:
    """Shield coinbase into `ua`'s Orchard receiver, which post-NU6.3 mints
    Ironwood notes. Thin wrapper over `shield_coinbase` with `pool=IRONWOOD`.
    Returns `(txid, ironwood_zat)`."""
    return shield_coinbase(node, wallet, taddr, ua, acct, Pool.IRONWOOD, extra)


def ironwood_outputs(view: dict) -> list[dict]:
    """The Ironwood outputs (pool == "ironwood") of a z_viewtransaction result."""
    return outputs_in_pool(view, Pool.IRONWOOD)


def ironwood_spends(view: dict) -> list[dict]:
    """The Ironwood spends (pool == "ironwood") of a z_viewtransaction result."""
    return spends_in_pool(view, Pool.IRONWOOD)
