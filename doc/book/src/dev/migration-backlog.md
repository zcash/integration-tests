# Test Migration Backlog

When the functional tests moved off `zcashd` onto the Z3 stack (`zebrad` +
`zainod` + `zallet`), the tests that did not yet work were parked in a
`DISABLED_SCRIPTS` list in `qa/pull-tester/rpc-tests.py` rather than ported.
That list reached 109 entries against 28 enabled tests and none of it had run
in CI for over a year.

`zcashd` has since reached end of life and been archived, so the tests can
never be run against the implementation they were written for, and the list
was removed. This page is the record of what it contained, so the coverage it
described is not silently lost.

The files themselves are recoverable from git history — they were deleted in
the commit that added this page.

The "last commit" column is the year the file was last touched at all; many
2026 dates are mechanical framework-wide sweeps (import renames, enum
migrations) rather than real work on the test.

Two groups below are **not** merely historical: the tests blocked on a missing
Zallet or Zebra RPC describe coverage this repository still wants, and each
names the RPC that is missing. Treat them as a to-do list for upstream.

## Still disabled, still present

These stayed in the tree because they target the Z3 stack and are blocked on a
specific tracked bug rather than on the migration:

| Test | Blocked on |
| --- | --- |
| `zcashd_key_import.py`, `zcashd_key_import_db.py` | A librustzcash regression in `migrate-zcashd-wallet`: a legacy standalone transparent key whose address is already an account-derived receiver re-inserts that address, violating the UNIQUE index on `addresses.cached_transparent_receiver_address`. |
| `addnode.py` | Zebra regtest peering stalls in multi-peer topologies (zebra #10329, #10332). |
| `wallet_ironwood_reorg.py`, `wallet_ironwood_birthday.py` | Zallet zebra-backend: `wait_for_wallet_sync` never converges (zallet #560, #563, #576). |

## Deprecated zcashd wallet RPCs (51)

These called `getnewaddress`, `z_getnewaddress`, `getbalance`, `sendtoaddress`, `gettransaction` or `signrawtransaction`. Zallet replaces them with the account/UA API (`z_getaddressforaccount`, `z_getbalances`, `z_sendmany`, `z_viewtransaction`) and PCZT.

| Test | Last commit | Note |
| --- | --- | --- |
| `addressindex.py` | 2024 | getnewaddress->z_getaddressforaccount, getbalance->z_getbalances |
| `coinbase_funding_streams.py` | 2025 | getnewaddress->z_getaddressforaccount, z_getnewaddress->z_getaddressforaccount |
| `finalorchardroot.py` | 2024 | getnewaddress->z_getaddressforaccount, z_getnewaddress->z_getaddressforaccount |
| `finalsaplingroot.py` | 2024 | getnewaddress->z_getaddressforaccount, z_getnewaddress->z_getaddressforaccount |
| `getblocktemplate.py` | 2025 | getnewaddress->z_getaddressforaccount, z_getbalance->z_getbalances |
| `getrawtransaction_insight.py` | 2022 | getnewaddress->z_getaddressforaccount, sendtoaddress->z_sendmany |
| `keypool.py` | 2024 | getnewaddress->z_getaddressforaccount, encryptwallet->walletpassphrase/walletlock |
| `listtransactions.py` | 2022 | getnewaddress->z_getaddressforaccount, sendtoaddress->z_sendmany |
| `mempool_limit.py` | 2024 | z_getnewaddress->z_getaddressforaccount |
| `mempool_nu_activation.py` | 2026 | getnewaddress->z_getaddressforaccount, z_getnewaddress->z_getaddressforaccount |
| `mempool_packages.py` | 2025 | getnewaddress->z_getaddressforaccount, signrawtransaction->PCZT (wallet#99) |
| `mempool_reorg.py` | 2024 | getnewaddress->z_getaddressforaccount, signrawtransaction->PCZT (wallet#99) |
| `mempool_resurrect_test.py` | 2024 | getnewaddress->z_getaddressforaccount, gettransaction->z_viewtransaction |
| `mempool_spendcoinbase.py` | 2024 | getnewaddress->z_getaddressforaccount, signrawtransaction->PCZT (wallet#99) |
| `mempool_tx_expiry.py` | 2024 | getnewaddress->z_getaddressforaccount, z_getnewaddress->z_getaddressforaccount |
| `mergetoaddress_mixednotes.py` | 2024 | getnewaddress->z_getaddressforaccount, z_getnewaddress->z_getaddressforaccount |
| `mergetoaddress_sapling.py` | 2023 | z_getnewaddress->z_getaddressforaccount |
| `merkle_blocks.py` | 2024 | getnewaddress->z_getaddressforaccount, getbalance->z_getbalances |
| `mining_shielded_coinbase.py` | 2024 | getnewaddress->z_getaddressforaccount, z_getnewaddress->z_getaddressforaccount |
| `prioritisetransaction.py` | 2024 | getnewaddress->z_getaddressforaccount, getbalance->z_getbalances |
| `rawtransactions.py` | 2023 | getnewaddress->z_getaddressforaccount, getbalance->z_getbalances |
| `remove_sprout_shielding.py` | 2024 | getnewaddress->z_getaddressforaccount, z_getnewaddress->z_getaddressforaccount |
| `rest.py` | 2022 | getnewaddress->z_getaddressforaccount, getbalance->z_getbalances |
| `shorter_block_times.py` | 2024 | z_getnewaddress->z_getaddressforaccount |
| `show_help.py` | 2026 | getnewaddress->z_getaddressforaccount, z_getnewaddress->z_getaddressforaccount |
| `signrawtransactions.py` | 2026 | signrawtransaction->PCZT (wallet#99) |
| `spentindex.py` | 2022 | getnewaddress->z_getaddressforaccount, sendtoaddress->z_sendmany |
| `turnstile.py` | 2024 | z_getnewaddress->z_getaddressforaccount, z_getbalance->z_getbalances |
| `txn_doublespend.py` | 2024 | getnewaddress->z_getaddressforaccount, getbalance->z_getbalances |
| `wallet_anchorfork.py` | 2024 | z_getnewaddress->z_getaddressforaccount, getbalance->z_getbalances |
| `wallet_broadcast.py` | 2022 | getnewaddress->z_getaddressforaccount, getbalance->z_getbalances |
| `wallet_deprecation.py` | 2025 | getnewaddress->z_getaddressforaccount, z_getnewaddress->z_getaddressforaccount |
| `wallet_doublespend.py` | 2024 | z_getbalanceforaccount->z_getbalances, gettransaction->z_viewtransaction |
| `wallet_golden_5_6_0.py` | 2023 | z_getbalanceforaccount->z_getbalances |
| `wallet_isfromme.py` | 2025 | getnewaddress->z_getaddressforaccount, z_getnewaddress->z_getaddressforaccount |
| `wallet_listreceived.py` | 2024 | getnewaddress->z_getaddressforaccount, z_getnewaddress->z_getaddressforaccount |
| `wallet_listunspent.py` | 2024 | getnewaddress->z_getaddressforaccount, getbalance->z_getbalances |
| `wallet_orchard_change.py` | 2024 | z_getbalanceforaccount->z_getbalances |
| `wallet_orchard_persistence.py` | 2024 | z_getbalanceforaccount->z_getbalances |
| `wallet_orchard_reindex.py` | 2024 | z_getbalanceforaccount->z_getbalances |
| `wallet_overwintertx.py` | 2024 | getnewaddress->z_getaddressforaccount, z_getnewaddress->z_getaddressforaccount |
| `wallet_parsing_amounts.py` | 2023 | getnewaddress->z_getaddressforaccount, z_getnewaddress->z_getaddressforaccount |
| `wallet_sendmany_any_taddr.py` | 2024 | getnewaddress->z_getaddressforaccount, z_getnewaddress->z_getaddressforaccount |
| `wallet_shieldcoinbase_sapling.py` | 2022 | z_getnewaddress->z_getaddressforaccount, z_getbalance->z_getbalances |
| `wallet_shieldcoinbase_ua_nu5.py` | 2022 | z_getbalance->z_getbalances, z_getbalanceforaccount->z_getbalances |
| `wallet_shieldcoinbase_ua_sapling.py` | 2022 | z_getbalance->z_getbalances, z_getbalanceforaccount->z_getbalances |
| `wallet_treestate.py` | 2024 | z_getnewaddress->z_getaddressforaccount, z_getbalance->z_getbalances |
| `wallet_zero_value.py` | 2022 | getnewaddress->z_getaddressforaccount, signrawtransaction->PCZT (wallet#99) |
| `wallet_zip317_default.py` | 2024 | getnewaddress->z_getaddressforaccount, z_getnewaddress->z_getaddressforaccount |
| `zapwallettxes.py` | 2024 | getnewaddress->z_getaddressforaccount, getbalance->z_getbalances |
| `zmq_test.py` | 2022 | getnewaddress->z_getaddressforaccount, sendtoaddress->z_sendmany |

## Blocked on a missing Zallet RPC (20)

**These track real upstream gaps.** Each names the zcashd RPC that Zallet has no equivalent for yet.

| Test | Last commit | Note |
| --- | --- | --- |
| `fundrawtransaction.py` | 2023 | no Zallet equivalent: importpubkey |
| `key_import_export.py` | 2022 | no Zallet equivalent: dumpprivkey, importprivkey |
| `signrawtransaction_offline.py` | 2024 | no Zallet equivalent: dumpprivkey |
| `sprout_sapling_migration.py` | 2024 | no Zallet equivalent: z_importkey |
| `threeofthreerestore.py` | 2022 | no Zallet equivalent: dumpprivkey, importprivkey |
| `wallet_1941.py` | 2024 | no Zallet equivalent: z_exportkey, z_importkey |
| `wallet_accounts.py` | 2024 | no Zallet equivalent: z_exportviewingkey |
| `wallet_addresses.py` | 2022 | no Zallet equivalent: z_exportkey, z_importkey |
| `wallet_changeindicator.py` | 2024 | no Zallet equivalent: z_exportviewingkey |
| `wallet_import_export.py` | 2022 | no Zallet equivalent: z_exportkey, z_importkey |
| `wallet_listnotes.py` | 2024 | no Zallet equivalent: z_exportviewingkey |
| `wallet_nullifiers.py` | 2024 | no Zallet equivalent: z_exportkey, z_importkey, z_exportviewingkey |
| `wallet_orchard.py` | 2024 | no Zallet equivalent: resendwallettransactions |
| `wallet_orchard_init.py` | 2024 | no Zallet equivalent: resendwallettransactions |
| `wallet_persistence.py` | 2024 | no Zallet equivalent: z_exportkey, z_importkey, z_exportviewingkey |
| `wallet_sapling.py` | 2024 | no Zallet equivalent: z_exportkey, z_importkey, z_exportviewingkey |
| `wallet_shieldingcoinbase.py` | 2024 | no Zallet equivalent: z_exportviewingkey |
| `wallet_z_sendmany.py` | 2024 | no Zallet equivalent: z_exportviewingkey |
| `walletbackup.py` | 2022 | no Zallet equivalent: backupwallet |
| `zkey_import_export.py` | 2024 | no Zallet equivalent: z_exportkey, z_importkey |

## Bitcoin Core P2P/mininode framework (10)

Drove a node over the wire with the inherited `mininode`/`comptool` harness. Zebra does not speak to it.

| Test | Last commit | Note |
| --- | --- | --- |
| `bip65-cltv-p2p.py` | 2026 | P2P/mininode framework |
| `bipdersig-p2p.py` | 2026 | P2P/mininode framework |
| `feature_zip239.py` | 2026 | P2P/mininode framework |
| `invalidblockrequest.py` | 2026 | P2P/mininode framework |
| `invalidtxrequest.py` | 2026 | P2P/mininode framework |
| `p2p-fullblocktest.py` | 2026 | P2P/mininode framework |
| `p2p_node_bloom.py` | 2026 | P2P/mininode framework |
| `p2p_nu_peer_management.py` | 2026 | P2P/mininode framework |
| `p2p_txexpiringsoon.py` | 2026 | P2P/mininode framework |
| `p2p_txexpiry_dos.py` | 2026 | P2P/mininode framework |

## Pre-NU5 `-nuparams` CLI strings (7)

Passed zcashd `-nuparams=<branchid>:<height>` strings; the Z3 framework configures activation through `ZebraArgs.activation_heights`.

| Test | Last commit | Note |
| --- | --- | --- |
| `feature_zip221.py` | 2022 | pre-NU5 nuparams: migrate to ZebraArgs activation_heights |
| `feature_zip244_blockcommitments.py` | 2021 | pre-NU5 nuparams: migrate to ZebraArgs activation_heights |
| `orchard_reorg.py` | 2024 | pre-NU5 nuparams: migrate to ZebraArgs activation_heights |
| `post_heartwood_rollback.py` | 2016 | pre-NU5 nuparams: migrate to ZebraArgs activation_heights |
| `rewind_index.py` | 2022 | pre-NU5 nuparams: migrate to ZebraArgs activation_heights |
| `sapling_rewind_check.py` | 2022 | pre-NU5 nuparams: migrate to ZebraArgs activation_heights |
| `upgrade_golden.py` | 2025 | pre-NU5 nuparams: migrate to ZebraArgs activation_heights |

## Blocked on a missing Zebra RPC (4)

**These track real upstream gaps.**

| Test | Last commit | Note |
| --- | --- | --- |
| `blockchain.py` | 2026 | zebra missing gettxoutsetinfo |
| `errors.py` | 2026 | zebra missing gettxoutsetinfo |
| `getchaintips.py` | 2022 | zebra missing getchaintips |
| `nodehandling.py` | 2022 | zebra missing setban, listbanned, clearbanned |

## Other (12)

Assorted zcashd-specific assumptions: basic auth vs cookie auth, `-proxy`/tor, `-insightexplorer`/`-txindex`, `-anchorconfirmations`, the zcashd `regtest/debug.log` and `wallet.dat` datadir layout.

| Test | Last commit | Note |
| --- | --- | --- |
| `feature_logging.py` | 2026 | no zcashd regtest/debug.log |
| `feature_walletfile.py` | 2022 | zcashd wallet.dat/-wallet |
| `framework.py` | 2026 | no zcashd regtest/debug.log |
| `httpbasics.py` | 2026 | RPC basic auth (zebra uses cookie auth) |
| `mergetoaddress_ua_nu5.py` | 2023 | -anchorconfirmations unsupported |
| `mergetoaddress_ua_sapling.py` | 2023 | -anchorconfirmations unsupported |
| `multi_rpc.py` | 2026 | RPC basic auth (zebra uses cookie auth) |
| `proxy_test.py` | 2022 | -proxy/tor unsupported |
| `reorg_limit.py` | 2020 | investigate |
| `timestampindex.py` | 2019 | -insightexplorer index, -txindex unsupported |
| `wallet_tarnished_5_6_0.py` | 2023 | needs ZebraArgs migration (list args) |
| `wallet_unified_change.py` | 2024 | needs z_shieldcoinbase funding step: zallet z_sendmany cannot spend mined coinbase (have 0) |

---

Total removed: 104.

