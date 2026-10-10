"""Network upgrades, consensus branch IDs, and activation heights.

A consensus branch ID identifies the rules in force. It is mixed into every
sighash from Overwinter onwards, so a transaction signed for one branch cannot
be replayed on another: getting the branch ID wrong does not produce a rejected
transaction, it produces an invalid signature. That is why it is a type here
rather than a loose integer passed around, which is how the test framework
carried it.

Branch IDs and activation heights are taken from librustzcash
(components/zcash_protocol/src/consensus.rs), and the branch IDs agree with the
constants the test framework already carried in util.py.
"""

from __future__ import annotations

from enum import Enum
from typing import NewType

from pyzcash.errors import ZcashError

__all__ = [
    "BlockHeight",
    "Network",
    "NetworkUpgrade",
    "UnknownBranchIdError",
]

BlockHeight = NewType("BlockHeight", int)
"""A height in the block chain. The genesis block is height 0."""


class UnknownBranchIdError(ZcashError):
    """No known network upgrade has the given consensus branch ID."""


class NetworkUpgrade(Enum):
    """A Zcash network upgrade, in activation order.

    The members are ordered, and comparison follows that order, so
    ``NetworkUpgrade.NU5 >= NetworkUpgrade.CANOPY`` answers "are Canopy's rules
    in force by NU5".
    """

    SPROUT = 0
    OVERWINTER = 1
    SAPLING = 2
    BLOSSOM = 3
    HEARTWOOD = 4
    CANOPY = 5
    NU5 = 6
    NU6 = 7
    NU6_1 = 8
    NU6_2 = 9
    NU6_3 = 10
    NU7 = 11

    @property
    def branch_id(self) -> int:
        """The consensus branch ID that identifies this upgrade's rules."""
        return _BRANCH_IDS[self]

    @classmethod
    def from_branch_id(cls, branch_id: int) -> NetworkUpgrade:
        """Find the upgrade with this consensus branch ID.

        Raises:
            UnknownBranchIdError: if no known upgrade uses it. A transaction
                carrying an unknown branch ID was built for rules this library
                does not know, and must not be treated as if it were current.
        """
        try:
            return _BY_BRANCH_ID[branch_id]
        except KeyError:
            raise UnknownBranchIdError(
                f"no known network upgrade has consensus branch ID "
                f"0x{branch_id:08x}"
            ) from None

    def __lt__(self, other: NetworkUpgrade) -> bool:
        return self.value < other.value

    def __le__(self, other: NetworkUpgrade) -> bool:
        return self.value <= other.value

    def __gt__(self, other: NetworkUpgrade) -> bool:
        return self.value > other.value

    def __ge__(self, other: NetworkUpgrade) -> bool:
        return self.value >= other.value


_BRANCH_IDS: dict[NetworkUpgrade, int] = {
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
    NetworkUpgrade.NU7: 0xFFFFFFFF,
}

_BY_BRANCH_ID: dict[int, NetworkUpgrade] = {
    branch_id: upgrade for upgrade, branch_id in _BRANCH_IDS.items()
}

# Sprout is the original rule set, in force from genesis, so it needs no
# activation height. NU7 has not been assigned one on any public network.
_MAINNET_ACTIVATIONS: dict[NetworkUpgrade, int] = {
    NetworkUpgrade.OVERWINTER: 347_500,
    NetworkUpgrade.SAPLING: 419_200,
    NetworkUpgrade.BLOSSOM: 653_600,
    NetworkUpgrade.HEARTWOOD: 903_000,
    NetworkUpgrade.CANOPY: 1_046_400,
    NetworkUpgrade.NU5: 1_687_104,
    NetworkUpgrade.NU6: 2_726_400,
    NetworkUpgrade.NU6_1: 3_146_400,
    NetworkUpgrade.NU6_2: 3_364_600,
    NetworkUpgrade.NU6_3: 3_428_143,
}

_TESTNET_ACTIVATIONS: dict[NetworkUpgrade, int] = {
    NetworkUpgrade.OVERWINTER: 207_500,
    NetworkUpgrade.SAPLING: 280_000,
    NetworkUpgrade.BLOSSOM: 584_000,
    NetworkUpgrade.HEARTWOOD: 903_800,
    NetworkUpgrade.CANOPY: 1_028_500,
    NetworkUpgrade.NU5: 1_842_420,
    NetworkUpgrade.NU6: 2_976_000,
    NetworkUpgrade.NU6_1: 3_536_500,
    NetworkUpgrade.NU6_2: 4_052_000,
    NetworkUpgrade.NU6_3: 4_134_000,
}


class Network(Enum):
    """A Zcash network.

    Regtest has no fixed activation heights: a test chooses them, which is what
    the ``-nuparams`` flags in the integration suite do. Asking regtest for an
    activation height therefore raises rather than inventing an answer.
    """

    MAIN = "main"
    TEST = "test"
    REGTEST = "regtest"

    def activation_height(self, upgrade: NetworkUpgrade) -> BlockHeight | None:
        """The height at which ``upgrade`` takes effect.

        Returns:
            The activation height, or None if the upgrade has not been
            scheduled on this network (NU7, as of writing). Sprout is in force
            from genesis and returns height 0.

        Raises:
            ZcashError: on regtest, whose schedule is chosen per test rather
                than fixed by the network.
        """
        if upgrade is NetworkUpgrade.SPROUT:
            return BlockHeight(0)
        if self is Network.MAIN:
            height = _MAINNET_ACTIVATIONS.get(upgrade)
        elif self is Network.TEST:
            height = _TESTNET_ACTIVATIONS.get(upgrade)
        else:
            raise ZcashError(
                "regtest has no fixed activation heights; a test chooses them "
                "(this is what -nuparams does). Build an explicit schedule and "
                "call branch_id_for_height with it instead."
            )
        return None if height is None else BlockHeight(height)

    def upgrade_active_at(self, height: BlockHeight) -> NetworkUpgrade:
        """The latest upgrade whose rules are in force at ``height``."""
        if height < 0:
            raise ValueError(f"block height cannot be negative: {height}")
        active = NetworkUpgrade.SPROUT
        for upgrade in NetworkUpgrade:
            activation = self.activation_height(upgrade)
            if activation is not None and height >= activation:
                active = upgrade
        return active

    def branch_id_for_height(self, height: BlockHeight) -> int:
        """The consensus branch ID a transaction mined at ``height`` must use.

        This is the value that goes into the sighash. Get it wrong and the
        signature is invalid, not merely non-standard.
        """
        return self.upgrade_active_at(height).branch_id
