"""Scripts: the bytes, and the operations they parse into."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from pyzcash.errors import ParseError
from pyzcash.script.num import encode_script_num
from pyzcash.script.opcodes import Opcode

__all__ = [
    "MAX_SCRIPT_ELEMENT_SIZE",
    "MAX_SCRIPT_SIZE",
    "Operation",
    "Script",
    "ScriptItem",
]

MAX_SCRIPT_SIZE = 10_000
MAX_SCRIPT_ELEMENT_SIZE = 520
"""The largest value a single push may place on the stack."""

_DIRECT_PUSH_MAX = 0x4B
"""A leading byte of 0x01..0x4b pushes that many bytes directly."""

_PUSH_PREFIXES = frozenset(
    {Opcode.OP_PUSHDATA1, Opcode.OP_PUSHDATA2, Opcode.OP_PUSHDATA4}
)
"""These are followed by a length and its bytes; they cannot stand alone."""

ScriptItem = Opcode | bytes | int
"""What :meth:`Script.build` accepts.

An Opcode is emitted as itself; bytes become a minimal push of those bytes; an
int becomes a push of its script-number encoding. The distinction matters: to
push the *number* 1 write ``1``, and to emit the *opcode* OP_1 write
``Opcode.OP_1``. They encode to the same byte here, but they do not in general.
"""


@dataclass(frozen=True, slots=True)
class Operation:
    """One parsed step of a script.

    ``data`` is set exactly when the operation pushes bytes onto the stack. A
    push of 5 bytes has ``opcode`` equal to 5 (the direct-push length byte),
    which is not a member of :class:`Opcode`, so ``opcode`` is a plain int.
    """

    opcode: int
    data: bytes | None = None

    @property
    def is_push(self) -> bool:
        return self.data is not None

    def __repr__(self) -> str:
        if self.data is not None:
            return f"push({self.data.hex()})"
        try:
            return Opcode(self.opcode).name
        except ValueError:
            return f"OP_UNKNOWN(0x{self.opcode:02x})"


@dataclass(frozen=True, slots=True)
class Script:
    """A script, as raw bytes plus the ability to read them as operations.

    Kept as bytes rather than a list of operations, because a script's bytes are
    what gets hashed and signed. Two different byte strings can parse to the
    same operations (a value can be pushed with OP_PUSHDATA1 instead of a direct
    push, say), and re-serializing from parsed operations would silently
    normalize that, changing the txid.
    """

    raw: bytes = b""

    @classmethod
    def build(cls, *items: ScriptItem) -> Self:
        """Assemble a script from opcodes, byte pushes, and numbers.

        Raises:
            ValueError: on a bare OP_PUSHDATA1, OP_PUSHDATA2, or OP_PUSHDATA4.
                Those are push *prefixes*, not standalone opcodes: each must be
                followed by a length and that many bytes. Emitting one alone
                produces a script that can never parse, so it is refused here
                rather than built. Pass the bytes to push instead, and the
                right prefix is chosen automatically.
        """
        out = bytearray()
        for item in items:
            if isinstance(item, Opcode):
                if item in _PUSH_PREFIXES:
                    raise ValueError(
                        f"{item.name} is a push prefix, not an opcode that can "
                        f"stand alone; pass the bytes to push instead"
                    )
                out.append(int(item))
            elif isinstance(item, bytes):
                out += _encode_push(item)
            elif isinstance(item, int):
                out += _encode_push(encode_script_num(item))
            else:
                raise TypeError(f"cannot put {type(item).__name__} in a script")
        return cls(bytes(out))

    @classmethod
    def from_hex(cls, hex_str: str) -> Self:
        return cls(bytes.fromhex(hex_str))

    def to_hex(self) -> str:
        return self.raw.hex()

    def operations(self) -> list[Operation]:
        """Parse the script into operations.

        Raises:
            ParseError: if a push runs off the end of the script. Such a script
                is unparseable, not merely unspendable, so it cannot be
                summarized honestly and this raises instead of guessing.
        """
        ops: list[Operation] = []
        i = 0
        raw = self.raw
        n = len(raw)

        while i < n:
            opcode = raw[i]
            i += 1

            if opcode == 0 or opcode > _DIRECT_PUSH_MAX:
                if opcode in (
                    Opcode.OP_PUSHDATA1,
                    Opcode.OP_PUSHDATA2,
                    Opcode.OP_PUSHDATA4,
                ):
                    size_len = {
                        Opcode.OP_PUSHDATA1: 1,
                        Opcode.OP_PUSHDATA2: 2,
                        Opcode.OP_PUSHDATA4: 4,
                    }[Opcode(opcode)]
                    if i + size_len > n:
                        name = Opcode(opcode).name
                        raise ParseError(f"truncated {name} length at byte {i}")
                    length = int.from_bytes(raw[i : i + size_len], "little")
                    i += size_len
                    if i + length > n:
                        raise ParseError(
                            f"{Opcode(opcode).name} claims {length} bytes but "
                            f"only {n - i} remain"
                        )
                    ops.append(Operation(opcode, raw[i : i + length]))
                    i += length
                else:
                    ops.append(Operation(opcode))
            else:
                if i + opcode > n:
                    raise ParseError(
                        f"push of {opcode} bytes at offset {i - 1} runs past "
                        f"the end of the script"
                    )
                ops.append(Operation(opcode, raw[i : i + opcode]))
                i += opcode

        return ops

    def __len__(self) -> int:
        return len(self.raw)

    def __bool__(self) -> bool:
        return bool(self.raw)

    def __repr__(self) -> str:
        try:
            return (
                "Script(" + " ".join(repr(op) for op in self.operations()) + ")"
            )
        except ParseError:
            return f"Script(<unparseable: {self.raw.hex()}>)"


def _encode_push(data: bytes) -> bytes:
    """Encode a push of ``data``, in the shortest form that fits it."""
    length = len(data)
    if length < Opcode.OP_PUSHDATA1:
        return bytes([length]) + data
    if length <= 0xFF:
        return bytes([Opcode.OP_PUSHDATA1, length]) + data
    if length <= 0xFFFF:
        return (
            bytes([Opcode.OP_PUSHDATA2]) + length.to_bytes(2, "little") + data
        )
    if length <= 0xFFFFFFFF:
        return (
            bytes([Opcode.OP_PUSHDATA4]) + length.to_bytes(4, "little") + data
        )
    raise ValueError(f"cannot push {length} bytes")
