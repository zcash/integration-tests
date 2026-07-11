"""Hypothesis strategies for pyzcash values.

Run the property tests that use these with:

    uv run pytest tests/test_properties.py

Kept in one module so the strategies are reusable and named. A strategy is a
generator of *arbitrary valid* values, which is what lets a property test say
something about every input rather than about the handful a person thought of.
"""

from __future__ import annotations

from hypothesis import strategies as st

from pyzcash.address import (
    Receiver,
    ReceiverType,
    SaplingAddress,
    TexAddress,
    TransparentAddress,
    TransparentKind,
    UnifiedAddress,
)
from pyzcash.consensus import MAX_MONEY, Network, Zatoshi
from pyzcash.encoding import MIN_LENGTH
from pyzcash.script import Opcode, Script
from pyzcash.transaction import (
    OrchardBundle,
    OutPoint,
    SaplingBundle,
    Transaction,
    TxIn,
    TxOut,
    TxVersion,
)

__all__ = [
    "amounts",
    "compact_sizes",
    "f4jumble_messages",
    "hrps",
    "networks",
    "receivers",
    "sapling_addresses",
    "script_numbers",
    "scripts",
    "tex_addresses",
    "transactions",
    "transparent_addresses",
    "transparent_txins",
    "transparent_txouts",
    "u32s",
    "unified_addresses",
]

# --- primitives -------------------------------------------------------------

u32s = st.integers(min_value=0, max_value=0xFFFFFFFF)

compact_sizes = st.integers(min_value=0, max_value=(1 << 64) - 1)

script_numbers = st.integers(min_value=-(2**31), max_value=2**31 - 1)

amounts = st.integers(min_value=-MAX_MONEY, max_value=MAX_MONEY).map(Zatoshi)

networks = st.sampled_from(list(Network))

# A Bech32 human-readable part: printable ASCII, lowercase, non-empty.
hrps = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=1,
    max_size=12,
).map(str.lower)

# F4Jumble's domain. The upper bound is capped well below ZIP 316's 2**22 + 64,
# because the permutation is O(n) in BLAKE2b calls and generating megabyte
# messages would make the suite slow without testing anything the shorter
# lengths do not. The lengths that matter (the odd ones below 128, where the
# left/right split is decided) are all reachable here.
_F4JUMBLE_TEST_MAX = 512
f4jumble_messages = st.binary(min_size=MIN_LENGTH, max_size=_F4JUMBLE_TEST_MAX)


# --- scripts ----------------------------------------------------------------

# OP_PUSHDATA1/2/4 are push *prefixes*, not standalone opcodes: Script.build
# refuses them, because emitting one alone yields bytes that can never parse.
# Pushing bytes selects the right prefix automatically, which the sizes below
# exercise (they cross the direct-push and OP_PUSHDATA1 boundaries).
_emittable_opcodes = [
    op
    for op in Opcode
    if op not in (Opcode.OP_PUSHDATA1, Opcode.OP_PUSHDATA2, Opcode.OP_PUSHDATA4)
]

_script_items = st.one_of(
    st.sampled_from(_emittable_opcodes),
    st.binary(max_size=600),
    script_numbers,
)

scripts = st.lists(_script_items, max_size=8).map(lambda xs: Script.build(*xs))


# --- transactions -----------------------------------------------------------

_outpoints = st.builds(
    OutPoint,
    txid=st.binary(min_size=32, max_size=32),
    index=u32s,
)

transparent_txins = st.builds(
    TxIn,
    prevout=_outpoints,
    script_sig=scripts,
    sequence=u32s,
)

transparent_txouts = st.builds(
    TxOut,
    value=st.integers(min_value=0, max_value=MAX_MONEY).map(Zatoshi),
    script_pubkey=scripts,
)


@st.composite
def transactions(
    draw: st.DrawFn, version: TxVersion | None = None
) -> Transaction:
    """A transparent-only transaction of any version.

    The shielded bundles are left empty: a valid Sapling or Orchard bundle is
    made of proofs and signatures, and generating random bytes of the right
    lengths would exercise the serializer without exercising anything true about
    Zcash. The shielded paths are covered instead by the canonical vectors in
    test_transaction.py, which are real bundles.
    """
    chosen = (
        version
        if version is not None
        else draw(st.sampled_from(list(TxVersion)))
    )
    return Transaction(
        version=chosen,
        lock_time=draw(u32s),
        expiry_height=draw(u32s) if chosen.has_expiry_height else None,
        consensus_branch_id=draw(u32s) if chosen is TxVersion.V5 else None,
        vin=tuple(draw(st.lists(transparent_txins, max_size=3))),
        vout=tuple(draw(st.lists(transparent_txouts, max_size=3))),
        sapling_bundle=SaplingBundle(),
        orchard_bundle=OrchardBundle(),
    )


# --- addresses --------------------------------------------------------------

_hash160s = st.binary(min_size=20, max_size=20)

transparent_addresses = st.builds(
    TransparentAddress,
    network=networks,
    kind=st.sampled_from(list(TransparentKind)),
    hash=_hash160s,
)

tex_addresses = st.builds(TexAddress, network=networks, hash=_hash160s)

sapling_addresses = st.builds(
    SaplingAddress,
    network=networks,
    diversifier=st.binary(min_size=11, max_size=11),
    pk_d=st.binary(min_size=32, max_size=32),
)

_RECEIVER_LENGTHS = {
    ReceiverType.P2PKH: 20,
    ReceiverType.P2SH: 20,
    ReceiverType.SAPLING: 43,
    ReceiverType.ORCHARD: 43,
}

receivers = st.sampled_from(list(ReceiverType)).flatmap(
    lambda t: st.builds(
        Receiver,
        type=st.just(t),
        data=st.binary(
            min_size=_RECEIVER_LENGTHS[t], max_size=_RECEIVER_LENGTHS[t]
        ),
    )
)


@st.composite
def unified_addresses(draw: st.DrawFn) -> UnifiedAddress:
    """A unified address that satisfies the ZIP 316 structural rules.

    Generating receivers freely and filtering would throw most candidates away,
    so the rules are built into the strategy instead: at most one transparent
    receiver, at least one shielded one, and ascending typecode order.
    """
    chosen: list[Receiver] = []

    transparent = draw(
        st.one_of(
            st.none(),
            st.sampled_from([ReceiverType.P2PKH, ReceiverType.P2SH]),
        )
    )
    if transparent is not None:
        chosen.append(
            Receiver(
                type=transparent, data=draw(st.binary(min_size=20, max_size=20))
            )
        )

    shielded = draw(
        st.lists(
            st.sampled_from([ReceiverType.SAPLING, ReceiverType.ORCHARD]),
            min_size=1,
            max_size=2,
            unique=True,
        )
    )
    for kind in shielded:
        chosen.append(
            Receiver(type=kind, data=draw(st.binary(min_size=43, max_size=43)))
        )

    chosen.sort(key=lambda r: r.typecode)
    return UnifiedAddress(network=draw(networks), receivers=tuple(chosen))
