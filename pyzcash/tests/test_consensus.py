"""Networks, upgrades, branch IDs, and amounts."""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import cast

import pytest

from pyzcash.consensus import (
    COIN,
    MAX_MONEY,
    BlockHeight,
    Network,
    NetworkUpgrade,
    UnknownBranchIdError,
    Zatoshi,
)
from pyzcash.errors import RangeError, ZcashError
from tests.helpers import mutate

# The branch IDs the test framework carries in qa/rpc-tests/test_framework/
# util.py. If the library and the framework ever disagree, one of them is
# signing transactions for the wrong rule set, so pin them here explicitly.
FRAMEWORK_BRANCH_IDS = {
    NetworkUpgrade.SPROUT: 0x00000000,
    NetworkUpgrade.OVERWINTER: 0x5BA81B19,
    NetworkUpgrade.SAPLING: 0x76B809BB,
    NetworkUpgrade.BLOSSOM: 0x2BB40E60,
    NetworkUpgrade.HEARTWOOD: 0xF5B9230B,
    NetworkUpgrade.CANOPY: 0xE9FF75A6,
    NetworkUpgrade.NU5: 0xC2D6D0B4,
    NetworkUpgrade.NU6: 0xC8E71055,
    NetworkUpgrade.NU6_1: 0x4DEC4DF0,
    NetworkUpgrade.NU6_2: 0x5437F330,
    NetworkUpgrade.NU6_3: 0x37A5165B,
}


@pytest.mark.parametrize(("upgrade", "branch_id"), FRAMEWORK_BRANCH_IDS.items())
def test_branch_ids_match_the_test_framework(
    upgrade: NetworkUpgrade, branch_id: int
) -> None:
    assert upgrade.branch_id == branch_id


def test_branch_ids_are_unique() -> None:
    ids = [u.branch_id for u in NetworkUpgrade]
    assert len(set(ids)) == len(ids)


def test_branch_id_round_trips() -> None:
    for upgrade in NetworkUpgrade:
        assert NetworkUpgrade.from_branch_id(upgrade.branch_id) is upgrade


def test_an_unknown_branch_id_is_an_error_not_a_guess() -> None:
    with pytest.raises(UnknownBranchIdError, match="0x0badf00d"):
        NetworkUpgrade.from_branch_id(0x0BADF00D)


def test_upgrades_are_ordered_by_activation() -> None:
    assert NetworkUpgrade.NU5 > NetworkUpgrade.CANOPY
    assert NetworkUpgrade.SPROUT < NetworkUpgrade.OVERWINTER
    assert NetworkUpgrade.NU6_3 >= NetworkUpgrade.NU6_1


@pytest.mark.parametrize(
    ("network", "height", "expected"),
    [
        # Mainnet, at each boundary and one block before it.
        (Network.MAIN, 0, NetworkUpgrade.SPROUT),
        (Network.MAIN, 347_499, NetworkUpgrade.SPROUT),
        (Network.MAIN, 347_500, NetworkUpgrade.OVERWINTER),
        (Network.MAIN, 419_199, NetworkUpgrade.OVERWINTER),
        (Network.MAIN, 419_200, NetworkUpgrade.SAPLING),
        (Network.MAIN, 1_687_104, NetworkUpgrade.NU5),
        (Network.MAIN, 2_726_400, NetworkUpgrade.NU6),
        (Network.MAIN, 3_428_143, NetworkUpgrade.NU6_3),
        (Network.MAIN, 99_999_999, NetworkUpgrade.NU6_3),  # NU7 is unscheduled
        # Testnet activates at different heights, which is the whole point of
        # keeping the schedule per network.
        (Network.TEST, 207_499, NetworkUpgrade.SPROUT),
        (Network.TEST, 207_500, NetworkUpgrade.OVERWINTER),
        (Network.TEST, 1_842_420, NetworkUpgrade.NU5),
    ],
)
def test_upgrade_active_at_height(
    network: Network, height: int, expected: NetworkUpgrade
) -> None:
    assert network.upgrade_active_at(BlockHeight(height)) is expected
    assert (
        network.branch_id_for_height(BlockHeight(height)) == expected.branch_id
    )


def test_nu7_has_no_activation_height_yet(
    mainnet: Network, testnet: Network
) -> None:
    assert mainnet.activation_height(NetworkUpgrade.NU7) is None
    assert testnet.activation_height(NetworkUpgrade.NU7) is None


def test_sprout_is_in_force_from_genesis(mainnet: Network) -> None:
    assert mainnet.activation_height(NetworkUpgrade.SPROUT) == 0


def test_regtest_refuses_to_invent_an_activation_schedule(
    regtest: Network,
) -> None:
    """Regtest heights come from -nuparams, so there is no right answer here."""
    with pytest.raises(ZcashError, match="no fixed activation heights"):
        regtest.activation_height(NetworkUpgrade.NU5)


def test_negative_heights_are_rejected(mainnet: Network) -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        mainnet.upgrade_active_at(BlockHeight(-1))


# --- amounts ---------------------------------------------------------------


def test_zec_conversion_round_trips() -> None:
    assert Zatoshi.from_zec("1.5") == Zatoshi(150_000_000)
    assert Zatoshi.from_zec(Decimal("0.00000001")) == Zatoshi(1)
    assert Zatoshi(150_000_000).to_zec() == Decimal("1.5")
    assert Zatoshi(COIN).to_zec() == Decimal("1")


def test_amounts_reject_floats() -> None:
    """0.1 ZEC is not representable in binary floating point.

    The type signature already forbids a float, so the runtime guard exists for
    the case the types cannot see: a value arriving from an untyped source, such
    as a JSON payload or an RPC response. The cast below simulates exactly that,
    rather than suppressing the type error, which would prove nothing.
    """
    from_untyped_source = cast("Decimal", 0.1)
    with pytest.raises(TypeError, match="float"):
        Zatoshi.from_zec(from_untyped_source)


def test_amounts_reject_sub_zatoshi_precision() -> None:
    with pytest.raises(RangeError, match="finer than one zatoshi"):
        Zatoshi.from_zec("0.000000001")


def test_amounts_are_bounded_by_max_money() -> None:
    Zatoshi(MAX_MONEY)  # the bound itself is allowed
    Zatoshi(-MAX_MONEY)  # value balances are signed
    with pytest.raises(RangeError, match="out of range"):
        Zatoshi(MAX_MONEY + 1)
    with pytest.raises(RangeError, match="out of range"):
        Zatoshi(-MAX_MONEY - 1)


def test_arithmetic_recheckes_the_range() -> None:
    """An amount that exists is an amount that is in range."""
    with pytest.raises(RangeError):
        Zatoshi(MAX_MONEY) + Zatoshi(1)
    with pytest.raises(RangeError):
        Zatoshi(MAX_MONEY) * 2


def test_arithmetic() -> None:
    assert Zatoshi(3) + Zatoshi(4) == Zatoshi(7)
    assert Zatoshi(3) - Zatoshi(4) == Zatoshi(-1)
    assert -Zatoshi(5) == Zatoshi(-5)
    assert Zatoshi(5) * 3 == Zatoshi(15)
    assert 3 * Zatoshi(5) == Zatoshi(15)
    assert Zatoshi(-1).is_negative


def test_amounts_are_ordered_and_hashable() -> None:
    assert sorted([Zatoshi(3), Zatoshi(-1), Zatoshi(2)]) == [
        Zatoshi(-1),
        Zatoshi(2),
        Zatoshi(3),
    ]
    assert len({Zatoshi(1), Zatoshi(1)}) == 1


def test_amounts_are_immutable() -> None:
    """Frozen at runtime, not merely by convention.

    Written through a helper taking ``object`` so that the assignment is a real
    runtime attempt rather than a type error the checker has been told to
    ignore.
    """
    amount = Zatoshi(1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        mutate(amount, "value", 2)


def test_bool_is_not_an_amount() -> None:
    """bool is an int in Python, which is the kind of thing to reject."""
    with pytest.raises(TypeError):
        Zatoshi(True)


def test_str_shows_zec() -> None:
    assert str(Zatoshi(150_000_000)) == "1.50000000 ZEC"
