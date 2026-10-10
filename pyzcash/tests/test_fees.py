"""ZIP 317 conventional fees.

Run:

    uv run pytest tests/test_fees.py
"""

from __future__ import annotations

import pytest

from pyzcash.consensus import Zatoshi
from pyzcash.fees import (
    GRACE_ACTIONS,
    MARGINAL_FEE,
    conventional_fee,
    conventional_fee_for,
    logical_actions,
)
from pyzcash.transaction import (
    SaplingBundle,
    SaplingOutput,
    SaplingSpend,
    Transaction,
    TxVersion,
)
from tests.json_vectors import load_vectors

ALL_TXS = [
    str(row["tx"])
    for name in ("zip_0143", "zip_0243", "zip_0244")
    for row in load_vectors(name)
]


def test_the_fee_never_falls_below_the_grace_floor() -> None:
    """A minimal transaction still costs something."""
    assert conventional_fee(0) == MARGINAL_FEE * GRACE_ACTIONS
    assert conventional_fee(1) == MARGINAL_FEE * GRACE_ACTIONS
    assert conventional_fee(GRACE_ACTIONS) == MARGINAL_FEE * GRACE_ACTIONS


def test_the_fee_is_linear_above_the_floor() -> None:
    """Past the floor, a transaction pays in proportion to the work it costs."""
    assert conventional_fee(3) == MARGINAL_FEE * 3
    assert conventional_fee(10) == MARGINAL_FEE * 10
    assert conventional_fee(10) == Zatoshi(50_000)


def test_the_fee_is_monotonic() -> None:
    """More work never costs less."""
    fees = [conventional_fee(n) for n in range(20)]
    assert fees == sorted(fees)


def _sapling_bundle(spends: int, outputs: int) -> SaplingBundle:
    """A Sapling bundle of the given shape. The contents do not matter here."""
    return SaplingBundle(
        spends=tuple(
            SaplingSpend(
                cv=bytes(32),
                nullifier=bytes(32),
                rk=bytes(32),
                proof=bytes(192),
                spend_auth_sig=bytes(64),
                anchor=bytes(32),
            )
            for _ in range(spends)
        ),
        outputs=tuple(
            SaplingOutput(
                cv=bytes(32),
                cmu=bytes(32),
                ephemeral_key=bytes(32),
                enc_ciphertext=bytes(580),
                out_ciphertext=bytes(80),
                proof=bytes(192),
            )
            for _ in range(outputs)
        ),
        binding_sig=bytes(64),
    )


def test_shielded_spends_and_outputs_are_indistinguishable() -> None:
    """A Sapling bundle prices the same whichever side its actions are on.

    The rule counts max(spends, outputs), not their sum, so three spends and one
    output cost exactly what one spend and three outputs cost. Were it
    otherwise, the fee itself would leak which of the two a transaction was
    doing, and the fee is public even when the bundle is not.
    """
    lopsided = Transaction(
        version=TxVersion.V4,
        expiry_height=0,
        sapling_bundle=_sapling_bundle(spends=3, outputs=1),
    )
    mirrored = Transaction(
        version=TxVersion.V4,
        expiry_height=0,
        sapling_bundle=_sapling_bundle(spends=1, outputs=3),
    )

    assert logical_actions(lopsided) == logical_actions(mirrored) == 3
    assert conventional_fee_for(lopsided) == conventional_fee_for(mirrored)


@pytest.mark.parametrize("tx_hex", ALL_TXS)
def test_every_canonical_transaction_has_a_fee(tx_hex: str) -> None:
    """The fee is computable for every real transaction, and is in range."""
    tx = Transaction.from_hex(tx_hex)
    actions = logical_actions(tx)
    assert actions >= 0

    fee = conventional_fee_for(tx)
    assert fee == conventional_fee(actions)
    assert fee >= MARGINAL_FEE * GRACE_ACTIONS


def test_a_negative_action_count_is_refused() -> None:
    with pytest.raises(ValueError, match="cannot have"):
        conventional_fee(-1)
