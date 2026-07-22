# Orchard -> Ironwood migration: integration-test scenario catalog

Status: PLAN (for review). Once agreed, each scenario becomes one test file under
`qa/rpc-tests/` driving the zallet pool-migration RPCs on a regtest node.

This catalog exists because the migration ships to production wallets, the mobile
wallet in particular, over an unbounded variety of real wallet shapes. A single
happy-path test (`wallet_ironwood_migration.py`) is not enough; we want one test
per real-world shape and edge case, mirroring the way the note-preparation work
enumerated its cases.

## 1. What the migration is (context the scenarios assert against)

- A migration moves an account's Orchard balance across the NU6.3 turnstile into
  Ironwood, in two phases: PREPARATION (split the balance into self-funding notes
  through same-pool sends-to-self) then TRANSFERS (one crossing per funding note).
- For a large or fragmented balance the preparation is MULTI-LAYER: a source note
  is fanned out through several dependent layers, where a later layer spends the
  feeder notes an earlier layer minted.
- Two-phase signing: transactions are pre-signed, then proved and broadcast later.

## 2. The anchor-bucket / layer-dependency invariant (first-class requirement)

This is the property the user called out and several scenarios must pin down.

- Layer N spends output (feeder) notes minted by layer N-1. Those feeders are only
  witnessable AFTER layer N-1 is mined (their note commitments must be in the
  commitment tree, and a valid Merkle path to a committed anchor requires a block).
  So layer N cannot be built, signed, or broadcast until layer N-1 has mined.
- Consequence: each layer lives in a DIFFERENT ANCHOR BUCKET. Layer N is built and
  signed against the tree state (anchor) that exists only after layer N-1 mines;
  it can never share an anchor with, or be batched into the same block-signing
  round as, its predecessor. The transfers form yet another bucket after the last
  preparation layer mines.
- The database/migration interface must reflect this ordering, not just store a
  flat list: each preparation transaction records its `layer`, its `depends_on`
  (the whole prior layer), and an `anchor_boundary`, and its state moves
  Planned -> Signed only once its dependencies are Mined. A later layer sitting in
  `Planned` with unmet dependencies is the on-disk representation of "not yet in an
  anchor bucket we can sign against."

  CONFIRMED (2026-07-18): layer N spends layer N-1's outputs, so layer N DEPENDS ON
  layer N-1, and layers are built/signed/mined in order 0,1,2,... The assertions
  are locked to this direction.

## 3. The mobile-wallet "what do I sign next" requirement

A mobile wallet cannot pre-sign the whole migration up front (later layers are not
yet witnessable) and may be backgrounded/killed between layers. So the migration
interface must let the wallet, at any time, answer for the user:

- Which transaction(s) are READY to sign/broadcast right now (Signed, deps mined,
  scheduled height reached)?
- Which are WAITING, and on what (e.g. "waiting for layer 1 to mine before layer 2
  can be built")?
- How many steps remain, and what is the current phase, so the UI can show a
  progress indicator and a "next action" prompt?
- After an app restart mid-migration, the same answers must be recoverable purely
  from persisted state (nothing in memory).

Several scenarios below assert this surface. If the current status/progress RPC
does not yet expose a machine-readable "next actions" list (as opposed to a human
string), that is an INTERFACE GAP to close first; see section 7.

## 4. Cross-cutting assertions (apply to most lifecycle scenarios)

Unless a scenario says otherwise, each end-to-end run asserts:
- Value conservation: Orchard drained to a small residual; Ironwood minted equals
  the crossed value minus fees; no value created or lost.
- Input-pool exclusivity: the resulting Ironwood notes derive from exactly one
  source pool (Orchard here); Sapling/transparent balances are untouched.
- Consent/preview: the preview's crossing amounts and layer/funding-note counts
  match what actually gets built and broadcast.
- Resumability: killing and restarting the wallet mid-run does not corrupt or
  duplicate; the migration continues from persisted state.
- Idempotent advance: calling advance when nothing is ready is a safe no-op.

## 5. Scenario catalog

Grouped by theme. Each entry: ID, persona/shape, setup, what it exercises, key
extra assertions. "single-layer" vs "multi-layer" is a consequence of the setup,
not an input.

### A. Personas and note distributions

- A1. Small holder, single clean note (single-layer).
  Setup: one ~1 ZEC Orchard note. Exercises the minimal happy path; preparation is
  one layer or empty (funding notes used directly). Baseline for the others.

- A2. Retail, one mid note (single or shallow multi-layer).
  Setup: one ~10-20 ZEC note. A handful of funding notes, likely one layer.

- A3. Whale, one large note (deep multi-layer via fan-out).
  Setup: one large note near a size that forces a balanced fan-out across 2-3
  layers. THE canonical multi-layer / anchor-bucket scenario. Asserts each layer
  in a distinct anchor bucket and mined in order before the next is signed.

- A4. Exchange, many medium notes (wide, possibly multi-layer).
  Setup: dozens of medium Orchard notes (a hot-wallet shape). Exercises a wide
  layer 0 with many parallel preparation transactions, then transfers. Asserts the
  interface can enumerate many ready-to-sign txs and that they broadcast correctly.

- A5. Dust-heavy wallet (fragmentation-driven multi-layer).
  Setup: many sub-funding-value dust notes plus a few larger ones. Dust is
  consolidated into feeders; exercises the dust-consolidation path and the extra
  layers it induces. Asserts dust is swept, not stranded.

- A6. One whale note + a tail of dust (mixed).
  Setup: a large note plus many tiny notes. Exercises the planner choosing between
  fanning the whale and consolidating the dust in the same migration.

- A7. Many tiny notes only, below single-crossing funding (edge of viability).
  Setup: many notes each too small to fund a crossing alone. Exercises whether the
  aggregate can still fund at least one crossing; asserts either a valid migration
  or a clean "nothing to migrate" if the net (after fees) is below the dust floor.

### B. Layer / anchor-bucket structure (the invariant, explicitly)

- B1. Two-layer preparation, anchor buckets asserted.
  Setup tuned to exactly 2 layers. Asserts: layer 1 is Planned (unsigned) until
  layer 0 fully mines; layer 1's built anchor differs from layer 0's; broadcasting
  layer 1 before layer 0 mines is impossible via the interface.

- B2. Three-plus-layer preparation, ordering asserted.
  Setup tuned to >=3 layers. Asserts strict mine-before-sign ordering across every
  layer and that the transfers form a final bucket after the last layer mines.

- B3. Interleaving is forbidden.
  Assert the interface never offers a layer-N tx as "ready" while any layer-(N-1)
  tx is unmined, even if some layer-N placeholders exist. Guards against a wallet
  broadcasting out of anchor-bucket order.

### C. Mobile-wallet UX: next-action tracking and resume

- C1. "What do I sign next" across the whole lifecycle.
  Drive the migration and, at every step, assert the interface's machine-readable
  next-actions list matches reality: exactly the ready txs are offered; waiting txs
  report the correct blocker (which layer must mine). This is the primary
  mobile-UX scenario.

- C2. Cold resume between layers.
  Start a migration, broadcast + mine layer 0, then simulate an app
  restart (fresh process reading only persisted state) and assert the wallet can
  recompute "next action" (build/sign layer 1) with nothing held in memory.

- C3. Resume after the process dies mid-broadcast.
  Kill after a tx is signed but before it is recorded as broadcast; on resume,
  assert no double-spend and the tx is correctly retried or recognized as already
  in the mempool/mined.

- C4. Progress reporting monotonicity.
  Assert phase/progress advances monotonically (never regresses across steps under
  normal operation) and reaches completed exactly when all txs are mined.

### D. Edge cases (deep-think)

- D1. Reorg of a mined preparation layer.
  A layer-0 tx is mined then reorged out. Assert the migration detects the rollback
  (the dependent layer is not advanced), and re-broadcasts / re-confirms rather
  than proceeding on a vanished anchor. HIGH VALUE, HIGH DIFFICULTY on regtest.

- D2. Transfer/layer transaction expires before broadcast.
  Advance slowly so a scheduled tx passes its expiry height. Assert the interface
  surfaces the expiry and the migration can re-sign/re-schedule rather than silently
  stalling.

- D3. Cancel mid-migration after some layers broadcast.
  Cancel once layer 0 is mined and layer 1 is pending. Assert cancel is honored,
  the state becomes terminal, in-flight/unbroadcast txs are abandoned cleanly, and
  already-crossed value is not lost or double-counted.

- D4. Stalled layer (tx not mining).
  A broadcast layer tx never mines (e.g. dropped). Assert the migration reports it
  is waiting on that specific tx and does not advance the next layer; the wallet can
  re-broadcast.

- D5. Concurrent deposit during migration.
  New Orchard notes arrive after the migration is committed. Assert they are NOT
  silently swept into the in-flight migration (the committed plan is fixed), and are
  available for a subsequent migration.

- D6. Balance exactly at the one-run drainable ceiling.
  Setup near max_parts * cap. Asserts behavior at the boundary (either a valid
  maximal migration or a clean bound), and that entropy/privacy degradation at the
  ceiling (documented separately) does not cause a build failure.

- D7. Balance requiring more than the supported layer/round budget.
  A balance that cannot be migrated in one run. Assert a clean, explanatory refusal
  (or a documented multi-run continuation), not a panic or a silent truncation.

- D8. Nothing to migrate.
  Empty or below-dust-floor Orchard balance. Assert a clean "nothing to migrate."

- D9. Source notes below minconf.
  Recently received, not yet confirmed enough. Assert they are excluded and the
  preview/plan reflects only spendable notes.

- D10. Pre-NU6.3 rejection.
  Attempt before activation. Assert a clear rejection naming the required upgrade.
  (Preview test already covers part of this; keep the lifecycle-level check.)

- D11. Read-only / watch-only account.
  No spending key. Assert start fails cleanly (cannot sign) with a clear error.

- D12. Double start guard.
  Start twice. Assert the second is refused while one is in progress, and allowed
  after the first reaches a terminal state.

- D13. Wrong / unknown migration id, and wrong pool pair.
  Assert advance/status/cancel on an unknown id, and start on an unsupported pool
  pair, are rejected. (Partly covered by the preview test; keep at lifecycle level.)

### E. Privacy, scheduling, and denominations

- E1. Transfer schedule is respected.
  Assert transfers are not all broadcast at once but at their scheduled heights
  (the privacy spread), and the chain tip must reach each height.

- E2. Denomination shape.
  Assert the crossing values follow the canonical 1-2-5 denomination structure the
  engine produces, so the on-chain footprint matches the intended anonymity model.

- E3. Multi-account isolation.
  Two accounts, migrate one. Assert the other account's notes and any concurrent
  migration are unaffected (state is per-account/keyed correctly).

## 6. Suggested implementation order (once agreed)

1. Foundations that de-risk the rest: A3 (canonical multi-layer), B1/B2
   (anchor-bucket ordering), C1/C2 (next-action + resume). These pin the core
   invariant and the mobile-UX surface.
2. Persona coverage: A1, A2, A4, A5, A6.
3. Edge cases by value: D3 (cancel), D8/D9/D10/D11/D12 (cheap, high coverage), D5
   (concurrent deposit), D4 (stall), D2 (expiry).
4. Hard/expensive last: D1 (reorg), D6/D7 (ceiling/over-budget), E1/E2 (privacy).

## 6b. Architecture: the logic lives in librustzcash, not zallet

The mobile wallet consumes the librustzcash crates (`zcash_pool_migration_backend`
and `zcash_pool_migration_sqlite`) DIRECTLY; it does not go through zallet. So the
migration state machine, the state-transition functions, the advance decision
("what do I sign/broadcast next"), and the per-transaction status/next-actions
view all live in the engine crate as the single source of truth. zallet is one
thin consumer: it performs the wallet I/O (proving, broadcasting, DB persistence,
key decryption) and serializes the engine's types for its JSON-RPC surface. The
mobile wallet performs the same I/O with its own primitives and reuses the exact
same engine decision + status functions.

Consequence for these scenarios: they drive the flow through zallet's RPCs on
regtest (that is our integration harness), but what they ASSERT (states,
anchor-bucket ordering, next-actions, resume) is behavior of the shared engine.
The engine should also carry unit tests for the pure decision/status logic; the
integration scenarios here exercise it end to end against a real chain.

The two existing tests (`wallet_ironwood_migration.py`, the canonical multi-layer
lifecycle, and `wallet_ironwood_migration_preview.py`) are the first two entries
of this harness. They, and every scenario below, are ACTIVATED in the test runner
(moved off the disabled list) once the catalog is complete and each is green.

## 7. Interface / DB gaps to resolve before/with these tests

Deep-thinking the mobile-wallet and anchor-bucket requirements surfaces likely
interface work that the tests will force us to do (to confirm with user):

- A machine-readable NEXT-ACTIONS query (IN PROGRESS, in the engine): the engine
  exposes, per transaction, {kind, layer, state, depends_on, ready, action,
  blocked_on, txid, mined_height} plus an advance-decision (next_step), so any
  consumer can render "sign next" / "waiting for layer N" deterministically from
  persisted state. zallet surfaces it via z_getpoolmigrationstatus; the mobile
  wallet calls the engine directly. C1/C3/D4 assert against it.
- Explicit anchor-bucket / layer exposure in status so a wallet (and B1-B3) can
  assert which bucket each tx belongs to and that ordering is enforced.
- A defined reorg/rollback contract (D1) and an expiry/re-sign contract (D2) in the
  interface, so wallets know how to recover.
- Confirm the dependency direction (section 2 NOTE) before locking assertions.

## 8. Agreed decisions (2026-07-18)

1. Layer dependency direction: layer N depends on layer N-1 (CONFIRMED).
2. Build the machine-readable next-actions surface NOW (section 7), then write the
   C-group and D4 against it. This is a prerequisite for the mobile-UX scenarios.
3. Scope: implement ALL scenarios in the first pass, including the hard ones (D1
   reorg, D6 ceiling, D7 over-budget).

Implementation approach: build the next-actions interface first, then land one
scenario per file, following the order in section 6 but covering the whole
catalog.
