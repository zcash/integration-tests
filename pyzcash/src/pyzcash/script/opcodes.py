"""Script opcodes.

The values are Bitcoin's, which Zcash inherited unchanged. Disabled opcodes
(OP_CAT, OP_MUL, and the rest) are present because a script may contain them:
this library reads scripts, it does not decide whether they are valid, and a
parser that silently dropped an opcode it disapproved of would misreport what a
transaction actually says.
"""

from __future__ import annotations

from enum import IntEnum

__all__ = ["Opcode"]


class Opcode(IntEnum):
    """A script opcode.

    Values below 0x4c are not listed: a byte in 0x01..0x4b is a direct push of
    that many bytes, not an opcode, and is handled by the script parser.
    """

    OP_0 = 0x00
    OP_PUSHDATA1 = 0x4C
    OP_PUSHDATA2 = 0x4D
    OP_PUSHDATA4 = 0x4E
    OP_1NEGATE = 0x4F
    OP_RESERVED = 0x50
    OP_1 = 0x51
    OP_2 = 0x52
    OP_3 = 0x53
    OP_4 = 0x54
    OP_5 = 0x55
    OP_6 = 0x56
    OP_7 = 0x57
    OP_8 = 0x58
    OP_9 = 0x59
    OP_10 = 0x5A
    OP_11 = 0x5B
    OP_12 = 0x5C
    OP_13 = 0x5D
    OP_14 = 0x5E
    OP_15 = 0x5F
    OP_16 = 0x60
    OP_NOP = 0x61
    OP_VER = 0x62
    OP_IF = 0x63
    OP_NOTIF = 0x64
    OP_VERIF = 0x65
    OP_VERNOTIF = 0x66
    OP_ELSE = 0x67
    OP_ENDIF = 0x68
    OP_VERIFY = 0x69
    OP_RETURN = 0x6A
    OP_TOALTSTACK = 0x6B
    OP_FROMALTSTACK = 0x6C
    OP_2DROP = 0x6D
    OP_2DUP = 0x6E
    OP_3DUP = 0x6F
    OP_2OVER = 0x70
    OP_2ROT = 0x71
    OP_2SWAP = 0x72
    OP_IFDUP = 0x73
    OP_DEPTH = 0x74
    OP_DROP = 0x75
    OP_DUP = 0x76
    OP_NIP = 0x77
    OP_OVER = 0x78
    OP_PICK = 0x79
    OP_ROLL = 0x7A
    OP_ROT = 0x7B
    OP_SWAP = 0x7C
    OP_TUCK = 0x7D
    OP_CAT = 0x7E
    OP_SUBSTR = 0x7F
    OP_LEFT = 0x80
    OP_RIGHT = 0x81
    OP_SIZE = 0x82
    OP_INVERT = 0x83
    OP_AND = 0x84
    OP_OR = 0x85
    OP_XOR = 0x86
    OP_EQUAL = 0x87
    OP_EQUALVERIFY = 0x88
    OP_RESERVED1 = 0x89
    OP_RESERVED2 = 0x8A
    OP_1ADD = 0x8B
    OP_1SUB = 0x8C
    OP_2MUL = 0x8D
    OP_2DIV = 0x8E
    OP_NEGATE = 0x8F
    OP_ABS = 0x90
    OP_NOT = 0x91
    OP_0NOTEQUAL = 0x92
    OP_ADD = 0x93
    OP_SUB = 0x94
    OP_MUL = 0x95
    OP_DIV = 0x96
    OP_MOD = 0x97
    OP_LSHIFT = 0x98
    OP_RSHIFT = 0x99
    OP_BOOLAND = 0x9A
    OP_BOOLOR = 0x9B
    OP_NUMEQUAL = 0x9C
    OP_NUMEQUALVERIFY = 0x9D
    OP_NUMNOTEQUAL = 0x9E
    OP_LESSTHAN = 0x9F
    OP_GREATERTHAN = 0xA0
    OP_LESSTHANOREQUAL = 0xA1
    OP_GREATERTHANOREQUAL = 0xA2
    OP_MIN = 0xA3
    OP_MAX = 0xA4
    OP_WITHIN = 0xA5
    OP_RIPEMD160 = 0xA6
    OP_SHA1 = 0xA7
    OP_SHA256 = 0xA8
    OP_HASH160 = 0xA9
    OP_HASH256 = 0xAA
    OP_CODESEPARATOR = 0xAB
    OP_CHECKSIG = 0xAC
    OP_CHECKSIGVERIFY = 0xAD
    OP_CHECKMULTISIG = 0xAE
    OP_CHECKMULTISIGVERIFY = 0xAF
    OP_NOP1 = 0xB0
    OP_NOP2 = 0xB1
    OP_NOP3 = 0xB2
    OP_NOP4 = 0xB3
    OP_NOP5 = 0xB4
    OP_NOP6 = 0xB5
    OP_NOP7 = 0xB6
    OP_NOP8 = 0xB7
    OP_NOP9 = 0xB8
    OP_NOP10 = 0xB9
    OP_SMALLINTEGER = 0xFA
    OP_PUBKEYS = 0xFB
    OP_PUBKEYHASH = 0xFD
    OP_PUBKEY = 0xFE
    OP_INVALIDOPCODE = 0xFF

    @property
    def is_small_int(self) -> bool:
        """True for OP_0 and OP_1 to OP_16, which push a number directly."""
        return self is Opcode.OP_0 or Opcode.OP_1 <= self <= Opcode.OP_16

    @property
    def small_int_value(self) -> int:
        """The number an OP_0 / OP_1..OP_16 opcode pushes."""
        if self is Opcode.OP_0:
            return 0
        if not self.is_small_int:
            raise ValueError(f"{self.name} does not push a small integer")
        return int(self) - (int(Opcode.OP_1) - 1)

    @staticmethod
    def for_small_int(value: int) -> Opcode:
        """The opcode that pushes ``value`` directly, for 0 <= value <= 16."""
        if value == 0:
            return Opcode.OP_0
        if not 1 <= value <= 16:
            raise ValueError(f"no opcode pushes {value} directly")
        return Opcode(int(Opcode.OP_1) + value - 1)
