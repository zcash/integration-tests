"""Properties of the consensus primitives: amounts, upgrades, branch IDs.

Run:

    uv run pytest tests/properties/test_consensus.py
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from pyzcash.consensus import (
    COIN,
    MAX_MONEY,
    BlockHeight,
    Network,
    NetworkUpgrade,
    Zatoshi,
)
from pyzcash.errors import ZcashError
from tests.strategies import amounts

# --- amounts ----------------------------------------------------------------


@given(amounts)
def test_amounts_round_trip_through_zec(amount: Zatoshi) -> None:
    assert Zatoshi.from_zec(amount.to_zec()) == amount


@given(amounts)
def test_zec_conversion_never_loses_a_zatoshi(amount: Zatoshi) -> None:
    zec = amount.to_zec()
    assert zec == Decimal(amount.value) / COIN
    assert Zatoshi.from_zec(zec).value == amount.value


@given(amounts, amounts)
def test_addition_stays_in_range_or_raises(a: Zatoshi, b: Zatoshi) -> None:
    """An amount that exists is an amount that is in range."""
    try:
        total = a + b
    except ZcashError:
        assert abs(a.value + b.value) > MAX_MONEY
        return
    assert total.value == a.value + b.value
    assert abs(total.value) <= MAX_MONEY


@given(amounts, amounts)
def test_addition_is_commutative(a: Zatoshi, b: Zatoshi) -> None:
    try:
        left = a + b
    except ZcashError:
        return
    assert left == b + a


@given(amounts)
def test_negation_is_an_involution(amount: Zatoshi) -> None:
    negated = -amount
    assert -negated == amount


@given(amounts, amounts)
def test_subtraction_undoes_addition(a: Zatoshi, b: Zatoshi) -> None:
    try:
        total = a + b
    except ZcashError:
        return
    assert total - b == a


# --- upgrades and branch IDs ------------------------------------------------


@given(st.sampled_from(list(NetworkUpgrade)))
def test_branch_ids_round_trip(upgrade: NetworkUpgrade) -> None:
    assert NetworkUpgrade.from_branch_id(upgrade.branch_id) is upgrade


@given(
    st.sampled_from([Network.MAIN, Network.TEST]),
    st.integers(min_value=0, max_value=10_000_000),
)
def test_the_active_upgrade_never_goes_backwards(
    network: Network, height: int
) -> None:
    """Rules only ever accumulate: a later block is never on an older branch."""
    here = network.upgrade_active_at(BlockHeight(height))
    later = network.upgrade_active_at(BlockHeight(height + 1))
    assert later >= here


@given(
    st.sampled_from([Network.MAIN, Network.TEST]),
    st.integers(min_value=0, max_value=10_000_000),
)
def test_the_branch_id_is_the_active_upgrades_branch_id(
    network: Network, height: int
) -> None:
    active = network.upgrade_active_at(BlockHeight(height))
    assert network.branch_id_for_height(BlockHeight(height)) == active.branch_id
