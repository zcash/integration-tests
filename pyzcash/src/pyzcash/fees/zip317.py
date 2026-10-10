"""ZIP 317: the conventional fee.

Before ZIP 317, a Zcash fee was a flat 1000 zatoshis regardless of how much work
the transaction imposed. That is not a price, it is a constant, and it made
grinding a chain of tiny inputs almost free while an ordinary payment overpaid.

ZIP 317 prices a transaction by its *logical actions*: the work it actually
costs the network. A transparent input or output is one action, and so is each
Sapling spend or output and each Orchard action, with the shielded pools counted
in a way that does not reveal which of a bundle's actions are real.

The fee is then the marginal fee times the number of logical actions, with a
floor of a few actions, so that a minimal payment still costs something and a
large one costs in proportion.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyzcash.consensus import Zatoshi

if TYPE_CHECKING:
    from pyzcash.transaction import Transaction

__all__ = [
    "GRACE_ACTIONS",
    "MARGINAL_FEE",
    "conventional_fee",
    "logical_actions",
]

MARGINAL_FEE = Zatoshi(5000)
"""The price of one logical action."""

GRACE_ACTIONS = 2
"""The fee floor, in actions: a transaction never costs less than this many."""

P2PKH_STANDARD_INPUT_SIZE = 150
P2PKH_STANDARD_OUTPUT_SIZE = 34


def logical_actions(tx: Transaction) -> int:
    """The number of logical actions a transaction imposes on the network.

    The transparent side is counted by *size* rather than by count, so that an
    unusually large input or output is charged for the work it really costs. The
    shielded side is counted so that spends and outputs are indistinguishable:
    a bundle with three spends and one output prices the same as one with one
    spend and three outputs, or the fee itself would leak which it was.
    """
    transparent_in = sum(
        (len(txin.script_sig.raw) + P2PKH_STANDARD_INPUT_SIZE - 1)
        // P2PKH_STANDARD_INPUT_SIZE
        for txin in tx.vin
    )
    transparent_out = sum(
        (len(txout.script_pubkey.raw) + P2PKH_STANDARD_OUTPUT_SIZE - 1)
        // P2PKH_STANDARD_OUTPUT_SIZE
        for txout in tx.vout
    )

    sapling = max(len(tx.sapling_bundle.spends), len(tx.sapling_bundle.outputs))
    orchard = len(tx.orchard_bundle.actions)

    # A JoinSplit is two inputs and two outputs by construction.
    sprout = 2 * len(tx.joinsplits)

    return max(transparent_in, transparent_out) + sprout + sapling + orchard


def conventional_fee(actions: int) -> Zatoshi:
    """The fee for a transaction with this many logical actions."""
    if actions < 0:
        raise ValueError(f"a transaction cannot have {actions} actions")
    return MARGINAL_FEE * max(GRACE_ACTIONS, actions)


def conventional_fee_for(tx: Transaction) -> Zatoshi:
    """The conventional fee for a transaction."""
    return conventional_fee(logical_actions(tx))
