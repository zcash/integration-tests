"""The sighash type: which parts of a transaction a signature commits to.

A signature does not have to cover the whole transaction. The sighash type says
what it covers, and therefore what anyone may still change afterwards without
invalidating it. Getting this wrong is not a parse error; it is a signature over
something other than what the signer meant.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Self

from pyzcash.errors import ParseError

__all__ = ["SigHashType", "SigHashUnit"]

_MASK = 0x1F
ANYONECANPAY = 0x80


class SigHashUnit(IntEnum):
    """The base sighash type, before the ANYONECANPAY flag."""

    ALL = 1
    """Commits to every output. Nothing about them may change."""

    NONE = 2
    """Commits to no output at all. Anyone may redirect the funds."""

    SINGLE = 3
    """Commits only to the output at the same index as this input."""


@dataclass(frozen=True, slots=True)
class SigHashType:
    """A base type, plus the ANYONECANPAY flag.

    ANYONECANPAY drops the commitment to the *other* inputs, so someone else can
    add their own input later. It is what makes a signature composable, and also
    what makes it dangerous to use without meaning to.
    """

    unit: SigHashUnit = SigHashUnit.ALL
    anyone_can_pay: bool = False

    @property
    def value(self) -> int:
        """The byte that is appended to a signature and hashed into it."""
        return int(self.unit) | (ANYONECANPAY if self.anyone_can_pay else 0)

    @classmethod
    def from_byte(cls, value: int) -> Self:
        """Parse a sighash byte.

        Raises:
            ParseError: if the low five bits are not a known base type. Bitcoin
                treats an unrecognized base type as SIGHASH_ALL; this library
                refuses rather than silently signing something broader than the
                byte asked for.
        """
        try:
            unit = SigHashUnit(value & _MASK)
        except ValueError:
            raise ParseError(
                f"unknown sighash base type 0x{value & _MASK:02x} in "
                f"0x{value:02x}"
            ) from None
        return cls(unit=unit, anyone_can_pay=bool(value & ANYONECANPAY))

    @property
    def signs_all_outputs(self) -> bool:
        return self.unit is SigHashUnit.ALL

    def __str__(self) -> str:
        name = self.unit.name
        return (
            f"SIGHASH_{name}|ANYONECANPAY"
            if self.anyone_can_pay
            else f"SIGHASH_{name}"
        )


SIGHASH_ALL = SigHashType(SigHashUnit.ALL)
SIGHASH_NONE = SigHashType(SigHashUnit.NONE)
SIGHASH_SINGLE = SigHashType(SigHashUnit.SINGLE)
