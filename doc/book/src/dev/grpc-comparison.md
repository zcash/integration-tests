# Bringing `grpc_comparison.py` Live

The `qa/rpc-tests/grpc_comparison.py` test compares Zainod and Lightwalletd by
asking both implementations the same `CompactTxStreamer` gRPC queries while
they are backed by the same Zebrad node.

This chapter documents the process that led to the test becoming stable, fast,
and usable in CI. It is intentionally more historical than the inline comments
in the test file: the goal is to explain not just what the fixture does, but
why it ended up that way.

## Goal

The original goal was straightforward:

1. Build a short regtest chain containing transparent, Sapling, and Orchard
   activity.
2. Submit that chain to Zebrad.
3. Start Zainod and Lightwalletd against the same Zebrad state.
4. Compare their responses method-by-method.

The hard part was step 1. A chain that is easy to describe is not necessarily a
chain that `zcashd`, Zebrad, Zainod, and Lightwalletd will all accept and index
reliably in the same test harness.

## What made this fixture tricky

Several interacting constraints shaped the final fixture:

- The test needs both Sapling and Orchard activity, including cross-pool sends.
- Standalone `zcashd` wallet behavior is sensitive to note selection and wallet
  state reloading, especially for Orchard spends.
- Zebrad and standalone `zcashd` do not build the fixture chain together over
  P2P in this harness, so the test submits raw blocks explicitly.
- Zainod must only start after Zebrad has loaded the full chain state, or it can
  fail during initial indexing. That behavior appears to be a Zaino bug: Zainod
  should wait for Zebra instead of crashing during startup.
- Regenerating proof-heavy shielded transactions on every run is too slow for a
  useful parity test.

Those constraints are why the final test uses:

- two standalone `zcashd` builder wallets,
- a two-stage cache,
- checkpoint-assisted Zebrad replay,
- and explicit startup ordering for Zebrad, Zainod, and Lightwalletd.

## Dead ends we had to eliminate

The final structure came from working through a series of failures.

Some of these failures appear to be upstream bugs rather than intended
behavior. When they are reproducible in isolation, they should be tracked
against the relevant implementation (`zcashd`, Zaino, or Zebra), and any
protocol ambiguity should be clarified in the corresponding specification or
ZIP before the implementations are updated.

### One-wallet chain construction was not reliable

The first versions tried to build the whole fixture from a single standalone
`zcashd` wallet. That produced multiple classes of failure:

- Sapling funds created on-chain were not always surfaced as spendable to the
  next `z_sendmany` call.
- Orchard cross-pool and follow-on Orchard spends could crash in wallet anchor
  handling. That looks like a `zcashd` bug and should be reported there if it
  can be reproduced outside this fixture.
- The wallet would often choose the same note pool for multiple test
  transactions, leading to duplicate-nullifier or "insufficient funds" errors.

The fix was to split responsibilities:

- `zcashd0` authors the transparent and Sapling side of the chain.
- `zcashd1` owns the Orchard accounts and authors Orchard spends.

That mirrors the separation already used by the working Orchard wallet tests.

### The "obvious" chain was still too sensitive to note selection

Even with two wallets, using a single Sapling note pool and a single Orchard
note pool made the test fragile. Some later spends depended on `zcashd`
selecting exactly the notes we expected.

The final fixture avoids that by creating separate source pools:

- one Sapling pool for the Sapling-to-Orchard funding transaction,
- one Sapling pool for later Sapling spends,
- one Orchard pool for the first Orchard spend,
- and one Orchard pool for later Orchard-originated transactions.

That is why the fixture has two Sapling funding steps and two Orchard funding
steps instead of a single minimal funding transaction for each pool.

### ZIP 317 fee assumptions mattered

Some cross-pool transactions that looked simple on paper were not satisfiable
with a hard-coded `ZIP_317_FEE`. That also appears to be a `zcashd` wallet-side
issue rather than an intended invariant of the fixture. This behavior is now
tracked upstream as [`zcash/zcash#6956`](https://github.com/zcash/zcash/issues/6956).

The fix was to compute fees for the actual transaction shape where needed using
`conventional_fee(...)`, while still keeping `ZIP_317_FEE` for the simpler
cases that matched the existing wallet tests.

### Standalone `zcashd` and Zebrad needed aligned activation behavior

The fixture uses standalone `zcashd` builders, then replays the resulting chain
into Zebrad. That only worked reliably once the test stopped assuming every
network upgrade should activate at height 1.

The stable layout is:

- Overwinter through Canopy at height 1
- NU5 and NU6 at height 2

That matches Zebrad's regtest expectations closely enough for the replayed chain
to be accepted and indexed consistently by the downstream services.

### Zainod startup ordering mattered

One important operational rule emerged from debugging:

Zainod and Lightwalletd must connect only after Zebrad has the full replayed or
restored chain loaded.

If Zainod starts too early, it can fail during initial indexing because the
state it expects is not fully available yet. The final test therefore:

1. restores or replays the Zebrad chain,
2. waits for Zebrad to report the expected tip height,
3. then starts Zainod and Lightwalletd,
4. then waits for both indexers to catch up.

This ordering is required, not cosmetic.
It also appears to expose a Zaino startup bug that should be tracked separately
from the parity test itself.

## The final fixture design

The chain that shipped in the live test is:

- Blocks `1..200`: transparent coinbase to a `taddr`
- Block `201`: transparent to Sapling funding
- Block `202`: second transparent to Sapling funding
- Block `203`: Sapling to Orchard
- Block `204`: Sapling to Sapling
- Block `205`: transparent to Orchard
- Block `206`: Orchard to Orchard
- Block `207`: Orchard to Sapling
- Block `208`: Sapling to transparent
- Block `209`: Orchard to transparent

This gives the parity checks:

- transparent address queries,
- tree state queries,
- block and block-range queries,
- Sapling activity,
- Orchard activity,
- and cross-pool activity.

Just as importantly, it does so with a chain that all four components in the
test setup can handle reproducibly.

## Why the test uses a two-stage cache

Proof generation and shielded transaction construction dominate runtime if the
fixture is rebuilt from scratch on every run.

The final design uses two cache layers:

- `qa/rpc-tests/cache/grpc_comparison_stage1/`
  Stores the expensive builder-wallet state after the initial 200-block chain
  and the two Sapling funding steps.
- `qa/rpc-tests/cache/grpc_comparison/`
  Stores the final Zebrad state and metadata used by the parity test itself.

These caches are generated artifacts. They are useful for local development and
CI acceleration, but they should not be committed to git because the binary
archives are hard to review and unnecessarily bloat repository history.

This split was useful because it let development continue even when the later
shielded transactions were still being debugged. Once the test was stable, it
also kept the normal runtime low.

Typical usage is:

```bash
uv run ./qa/zcash/grpc_comparison_tests.py
```

To rebuild the fixture and overwrite the caches:

```bash
uv run ./qa/zcash/grpc_comparison_tests.py --fresh
```

## Why checkpoint-assisted replay exists

The final test still builds the chain with standalone `zcashd`, then loads it
into Zebrad by submitting raw blocks. The test also writes a temporary
checkpoint file for the replayed chain before starting Zebrad.

That part exists because the builder chain and the validator chain are not being
grown together live over P2P. The checkpoint-assisted setup made Zebrad replay
stable enough for the indexer comparison to become routine.

## Compact block parity is strict

The parity test now compares `CompactBlock` responses exactly as returned by
each implementation. It does not normalize `protoVersion`, omit transparent
coinbase compact transactions, or otherwise rewrite the compact block payload
before comparing it.

That makes the failures noisier, but it is deliberate: any divergence between
Zainod and Lightwalletd should be surfaced directly in test output so it can be
understood and fixed rather than normalized away.

If a divergence turns out to reflect an underspecified part of the protocol
rather than an implementation bug, the right long-term fix is to clarify that
behavior in the relevant spec. For gRPC behavior, that likely means ZIP 307 or
the lightwallet protocol itself. After that, the implementation that does not
match the clarified spec should be fixed, and the parity test should keep
failing until that happens.

## Maintenance guidance

If this test starts failing again, the safest order of operations is:

1. Verify the normal cached path still passes.
2. Run with `--fresh` to determine whether the failure is in cache restore or
   fixture generation.
3. Check whether Zebrad reaches the expected tip before Zainod starts.
4. Check whether a failure is really a parity mismatch, or whether it is a
   wallet-construction problem in the standalone builder nodes.
5. Avoid simplifying the fixture unless the replacement has been verified across
   all four components.

The biggest lesson from bringing this test live is that "shortest chain" and
"most maintainable chain" were not the same thing. The stable fixture is a bit
more explicit than the original idealized version, but it is much easier to run,
cache, and reason about in CI.
