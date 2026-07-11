"""Zatoshi, the amount type.

The test framework passed amounts as bare ints and bare Decimals, with a
``zat()`` helper to convert and a ``COIN`` constant defined twice, in util.py
and in mininode.py. Nothing distinguished "8 ZEC" from "8 zatoshis", and
nothing range-checked either.

Here an amount is a type. It is always an integer number of zatoshis, because
that is what the consensus rules count, and float is never accepted: 0.1 ZEC is
not representable in binary floating point, and silently rounding someone's
money is not a thing this library will do.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Self

from pyzcash.errors import RangeError

__all__ = ["COIN", "MAX_MONEY", "Zatoshi"]

COIN = 100_000_000
"""Zatoshis in one ZEC."""

MAX_MONEY = 21_000_000 * COIN
"""The total supply, and the magnitude bound the consensus rules impose."""

_ZEC_PLACES = Decimal(1).scaleb(-8)


@dataclass(frozen=True, order=True, slots=True)
class Zatoshi:
    """An amount of ZEC, counted in zatoshis.

    Values may be negative: a Sapling or Orchard bundle's value balance is
    signed. The magnitude is bounded by :data:`MAX_MONEY`, which is the range
    the consensus rules permit, and every operation re-checks it, so an amount
    that exists is an amount that is in range.

    Example:
        >>> Zatoshi.from_zec("1.5") + Zatoshi(1)
        Zatoshi(150000001)
        >>> Zatoshi.from_zec("1.5").to_zec()
        Decimal('1.50000000')
    """

    value: int

    def __post_init__(self) -> None:
        if not isinstance(self.value, int) or isinstance(self.value, bool):
            raise TypeError(
                f"a Zatoshi is a whole number of zatoshis, got "
                f"{type(self.value).__name__}"
            )
        if abs(self.value) > MAX_MONEY:
            raise RangeError(
                f"amount out of range: {self.value} zatoshis exceeds "
                f"MAX_MONEY ({MAX_MONEY})"
            )

    @classmethod
    def from_zec(cls, zec: Decimal | int | str) -> Self:
        """Convert a ZEC amount to zatoshis.

        A float is rejected: it cannot represent most ZEC amounts exactly, so
        accepting one would mean silently rounding. Pass a str or a Decimal.

        Raises:
            RangeError: if the value is out of range, or is finer than one
                zatoshi (ZEC has eight decimal places, and no more).
        """
        if isinstance(zec, float):
            raise TypeError(
                "refusing to convert a float to an amount, because it cannot "
                "represent most ZEC values exactly; pass a str or a Decimal"
            )
        amount = Decimal(zec)
        zats = amount.scaleb(8)
        if zats != zats.to_integral_value():
            raise RangeError(
                f"{amount} ZEC is finer than one zatoshi, which is the "
                f"smallest unit that exists"
            )
        return cls(int(zats))

    def to_zec(self) -> Decimal:
        """Convert to ZEC, exactly, with all eight decimal places."""
        return (Decimal(self.value) / COIN).quantize(_ZEC_PLACES)

    @property
    def is_negative(self) -> bool:
        return self.value < 0

    def __add__(self, other: Zatoshi) -> Zatoshi:
        return Zatoshi(self.value + other.value)

    def __sub__(self, other: Zatoshi) -> Zatoshi:
        return Zatoshi(self.value - other.value)

    def __neg__(self) -> Zatoshi:
        return Zatoshi(-self.value)

    def __mul__(self, factor: int) -> Zatoshi:
        if not isinstance(factor, int) or isinstance(factor, bool):
            raise TypeError("an amount may only be scaled by a whole number")
        return Zatoshi(self.value * factor)

    __rmul__ = __mul__

    def __str__(self) -> str:
        return f"{self.to_zec()} ZEC"
