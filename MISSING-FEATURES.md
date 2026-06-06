# Z3 stack feature gaps (from disabled RPC tests)

Notes on the gaps that keep migrated zcashd tests disabled, grouped by the
component that needs the work. Ordered roughly by how many tests each
unblocks.

Verified against source: zallet RPC trait (wallet/zallet/.../json_rpc/
methods.rs), zebra RPC methods (zebra-rpc/src), and zebra regtest activation
logic (zebra-chain/.../testnet.rs). One earlier hypothesis was WRONG and is
corrected below (pre-NU5 heights ARE configurable in zebra regtest).

## zallet: missing legacy wallet RPCs (largest group, ~70 tests)

zallet implements a modern account/unified API only. These zcashd wallet
RPCs are not implemented, so the tests using them cannot run:

- getnewaddress, z_getnewaddress
- sendtoaddress, sendmany
- getbalance, z_getbalance, z_getbalanceforaccount
- gettransaction, getreceivedbyaddress, listreceivedbyaddress
- getrawchangeaddress, getunconfirmedbalance
- dumpprivkey, importprivkey, importpubkey, importaddress (transparent)
- z_exportkey, z_importkey, z_exportviewingkey
- encryptwallet, keypoolrefill, backupwallet
- resendwallettransactions

Implemented today (zallet jsonrpsee trait, verified in
zallet/src/components/json_rpc/methods.rs - 30 methods total):
z_getnewaccount, z_getaccount, z_getaddressforaccount, z_listaccounts,
z_listunifiedreceivers, z_listunspent, z_getbalances, z_gettotalbalance,
z_sendmany, z_shieldcoinbase, z_listtransactions, z_viewtransaction,
z_importaddress, z_listoperationids, z_getoperationstatus, z_getoperationresult,
z_getnotescount, z_recoveraccounts, z_converttex, getwalletinfo, listaddresses,
validateaddress, verifymessage, getrawtransaction, decoderawtransaction,
decodescript, walletpassphrase, walletlock, help, stop.
Note: wallet encryption exists via walletpassphrase/walletlock (not the
zcashd `encryptwallet` RPC). No legacy aliases in the compatibility layer.

## wallet funding: z_sendmany cannot spend mined coinbase (affects ~all wallet tests)

Confirmed by migrating wallet_unified_change.py and probing the wallet state.
The harness mines regtest coinbase to each wallet's miner address, which
zallet derives as the account's internal (change) transparent address
(generate_account_and_miner_address.rs: derive_internal_ivk().default_address(),
birthday height 0). After syncing 200 cached blocks, wallet 0 reports:

  z_getbalances(0) -> account 0 transparent.regular.spendable = 31875000000 zat
  (~318.75 ZEC), but total.spendable = 0.

A z_sendmany from the miner address with 'AllowRevealedSenders' fails with
"Insufficient balance (have 0, need ...)". So the mined coinbase is visible in
the transparent balance breakdown but is not spendable through z_sendmany. The
canonical path is z_shieldcoinbase (which zallet implements) to shield coinbase
into a shielded address first, then z_sendmany from there.

Consequence: migrating any wallet test is not a simple RPC rename. Each test
that funds accounts from coinbase needs an added z_shieldcoinbase funding step
plus the account-model rewrite (account_uuid instead of integer index,
z_getbalances reshape, null fee). Other zallet API specifics found while
migrating wallet_unified_change.py:

- z_getnewaccount requires an account_name argument (zcashd took none).
- z_getnewaccount returns {account_uuid, account: null}; key everything by
  account_uuid (the ZIP 32 index is not populated). z_getaddressforaccount and
  z_getbalances both use account_uuid.
- z_sendmany rejects an explicit fee ("the fee field must be null"); it always
  computes the ZIP 317 fee internally. Pass null and assert against the
  ZIP 317 conventional fee.
- z_getaddressforaccount with default receiver types tries to derive a
  transparent receiver and hits the transparent gap limit ("index 10") for a
  fresh account with no transparent funds; request explicit shielded receiver
  types (e.g. ['sapling','orchard']) instead.

## zebra: missing node RPCs

- gettxoutsetinfo - blockchain.py, errors.py
- getchaintips - getchaintips.py
- setban / listbanned / clearbanned / disconnectnode - nodehandling.py
  (also getpeerinfo peer count differs)
- signrawtransaction - signrawtransactions.py (used by ~13 tests total)

## zebra regtest: activation-height config (CORRECTED - was wrong)

Source check (zebra-chain testnet.rs: for_regtest + with_activation_heights):
pre-NU5 heights are NOT hardcoded. for_regtest only DEFAULTS unset upgrades
to height 1 (overwinter.or(1), sapling.or(overwinter), ...); a configured
pre-NU5 height is kept. ConfiguredActivationHeights deserializes keys
overwinter/sapling/blossom/heartwood/canopy (lowercase) and NU5/NU6/NU6.1
(uppercase, via serde rename); the only constraints are in-order and
height > 0. So these tests are NOT blocked by a missing zebra feature -- they
need migration (pass activation_heights via ZebraArgs with correctly-cased
names):

- feature_zip221.py, feature_zip244_blockcommitments.py, orchard_reorg.py,
  post_heartwood_rollback.py, rewind_index.py, sapling_rewind_check.py,
  upgrade_golden.py, mempool_nu_activation.py

The wallet ones among these are still blocked by the zallet NU5 coupling
below.

## zallet/zebra: per-test activation heights + shared cache

This is a FRAMEWORK plumbing gap, not a missing zallet/zebra feature: zallet
accepts any regtest_nuparams (RegTestNuParam parsing in zallet/src/network.rs)
and zebra accepts any activation_heights, but the harness uses a static
zallet.toml (NU5@1) and builds the shared chain cache once at NU5@1, so a
wallet test needing a different NU5 height can't make node and wallet agree.
Fix is in the test harness (thread per-test heights into both, and/or a
per-config cache). Blocks the wallet_orchard* family and others.

## zcashd-only behaviour (needs an equivalent or test rewrite)

- RPC HTTP basic auth (rpcuser/rpcpassword) - httpbasics.py, multi_rpc.py
- wallet.dat file + -wallet flag - feature_walletfile.py
- regtest/debug.log - feature_logging.py, framework.py
- -proxy / tor - proxy_test.py
- address/spent/timestamp indexes (-insightexplorer/-txindex) -
  addressindex.py, spentindex.py, timestampindex.py,
  getrawtransaction_insight.py
- -regtestshieldcoinbase - wallet_treestate.py, wallet_anchorfork.py, ...
- Sprout address generation / sprout key import - sprout_sapling_migration.py,
  remove_sprout_shielding.py, ...
- 5.6.0 golden/tarnished wallet files - wallet_golden_5_6_0.py,
  wallet_tarnished_5_6_0.py

## P2P / mininode framework

These use the in-Python P2P test framework (NodeConn), not yet exercised
against zebra (10 tests, by NodeConn usage):

- invalidblockrequest.py, invalidtxrequest.py, p2p-fullblocktest.py,
  p2p_node_bloom.py, p2p_nu_peer_management.py, p2p_txexpiringsoon.py,
  p2p_txexpiry_dos.py, bip65-cltv-p2p.py, bipdersig-p2p.py, feature_zip239.py

## Confirmed: zebra reorg / sync behaviour (reorg_limit.py)

reorg_limit.py is node-only (no wallet). After fixing it to use num_wallets=0
and NU5@1, setup succeeds, but the core check fails: after a network split
where node 2 builds a longer (competing) chain, reconnecting does not make
node 0 reorg to it (expected getblockcount 300).

Source check: zebra has MAX_BLOCK_REORG_HEIGHT = MIN_TRANSPARENT_COINBASE_
MATURITY - 1 = 99 (zebra-state/src/constants.rs); blocks past that depth are
finalized and cannot be rolled back, and zebra does NOT do zcashd's deep-reorg
safety *shutdown* (it just refuses to roll back). zcashd allows reorgs up to
99 blocks and aborts beyond. So the test's exact boundary (100-block reorg
should succeed; 101 should stop the node) and the shutdown message don't match
zebra's finalize-and-refuse behaviour. Behavioural difference, not a missing
RPC; test needs reworking for zebra semantics.

## Batch 2 findings (confirmed by running)

- zebra/zallet missing `signrawtransaction` RPC - signrawtransactions.py
- `-proxy` / tor not supported - proxy_test.py (hangs)
- RPC HTTP basic auth not supported - httpbasics.py, multi_rpc.py (hang)

Note: P2P / mininode tests can't be evaluated in this local python build
(missing `_dbm` C extension), so they stay categorised as "mininode
framework, untested vs zebra" rather than confirmed gaps:
invalidblockrequest.py, invalidtxrequest.py, p2p-fullblocktest.py,
p2p_node_bloom.py, p2p_nu_peer_management.py, bip65-cltv-p2p.py,
bipdersig-p2p.py.

## Note on layering

Many disabled tests crash first on the un-migrated `extra_args=[[...]]` list
("'list' object has no attribute 'miner_address'") in their own setup_network
before reaching the feature that actually blocks them. That first symptom is
a ZebraArgs migration task, not a missing feature; the feature gaps listed
above (legacy wallet RPCs, pre-NU5 activation heights, missing node RPCs,
zcashd-only behaviour) are the real blockers behind it, identified from each
test's RPC / nuparams usage.

## Wallet tests: rewrite-able vs genuinely blocked (of the 73 tagged
## "zallet missing")

Splitting the wallet-RPC group by whether zallet already has the capability
(under its account/unified API) or genuinely lacks it.

REWRITE-ABLE (~33): use only ops with a zallet equivalent, so the test code
can be moved to zallet (a model rewrite, not a rename: per-address ->
per-account/pool, bare -> unified addresses). Mappings:
- sendtoaddress / sendmany        -> z_sendmany
- getbalance / z_getbalance /
  z_getbalanceforaccount           -> z_gettotalbalance / z_getbalances
- getnewaddress / z_getnewaddress  -> z_getaddressforaccount (+ z_listunifiedreceivers)
Caveat: some of these 33 still carry a non-wallet blocker (e.g.
wallet_treestate uses -regtestshieldcoinbase, zmq_test needs ZMQ,
wallet_golden_5_6_0 loads a zcashd wallet file, mempool_nu_activation needs
pre-NU5 boundaries), so the truly-clean subset is smaller.

BLOCKED (~40): use an RPC zallet genuinely lacks. By feature:
- Sprout support                  15  (Sprout is deprecated; likely retire, not migrate)
- signrawtransaction              13
- key import/export
  (z_importkey/z_exportkey/
   z_exportviewingkey/dumpprivkey/
   importprivkey)                  ~bulk
- encryptwallet                    3
- resendwallettransactions         2
- keypoolrefill                    2
- getrawchangeaddress              2
- importpubkey / backupwallet      1 each

## zcashd deprecation -> zallet migration (verified in zcash doc/book)

Many "missing" wallet RPCs are *deprecated* in zcashd (see zcashd
doc/book/src/user/deprecation.md) and are replaced by the account/unified-
address API that zallet already implements. So most of these tests are a
migration, not a feature gap:

- getnewaddress, z_getnewaddress     -> z_getaddressforaccount (+ z_getnewaccount)
- z_listaddresses                    -> listaddresses
- z_getbalance, getbalance,
  z_getbalanceforaccount             -> z_getbalances / z_gettotalbalance
- sendtoaddress, sendmany            -> z_sendmany
- gettransaction                     -> z_viewtransaction
- encryptwallet                      -> walletpassphrase / walletlock
- getrawchangeaddress, keypoolrefill,
  settxfee                           -> n/a (account model / ZIP 317 fees)
- createrawtransaction,
  fundrawtransaction,
  signrawtransaction                 -> PCZT-based, zcash/wallet#99
- getnetworkhashps                   -> getnetworksolps

Genuinely no zallet equivalent yet (not just a rename):
- transparent key import/export: dumpprivkey, importprivkey, importpubkey
- shielded key import/export: z_importkey, z_exportkey, z_exportviewingkey
- resendwallettransactions, backupwallet
- Sprout (deprecated/removed)

## Trial migration outcome (wallet_unified_change.py)

A one-test trial confirmed the "REWRITE-ABLE" group above is not a simple RPC
rename. Even a test that already uses the modern account API needs a structural
rewrite before it can run on the Z3 stack:

1. Funding: insert a z_shieldcoinbase step. zallet z_sendmany cannot spend the
   harness-mined coinbase (see "wallet funding" section); shield it into a
   shielded address first, then z_sendmany from there.
2. Account identity: use account_uuid from z_getnewaccount, not the integer
   index (which is null). Pass the uuid to z_getaddressforaccount and match it
   in z_getbalances.
3. Balance asserts: reshape z_getbalanceforaccount's
   {pools:{sapling:{valueZat}}, minimum_confirmations} to z_getbalances'
   accounts[].{sapling,orchard}.spendable.valueZat.
4. Fees: pass null to z_sendmany and assert against the ZIP 317 conventional
   fee that zallet computes.
5. Addresses: request explicit shielded receiver types from
   z_getaddressforaccount to avoid the transparent gap limit.
6. Wiring: route wallet RPCs to self.wallets[i] (not self.nodes[i]); take the
   coinbase source from self.miner_addresses[i]; read confirmations via the
   node's getrawtransaction (zallet has no gettransaction).

The trial was stopped before completing the z_shieldcoinbase funding rewrite;
wallet_unified_change.py remains disabled. The steps above are the recipe for
whoever picks the wallet-test migration back up.
