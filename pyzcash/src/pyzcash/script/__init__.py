"""Scripts: opcodes, parsing, building, and the standard templates.

A transparent output does not name an address; it carries a script. This layer
reads and writes those scripts, and translates the two standard templates to and
from the addresses they pay to.

It does not execute scripts. Deciding whether a script is satisfied is the
node's job, and this library does not validate consensus.
"""

from __future__ import annotations

from pyzcash.script.num import (
    DEFAULT_MAX_LEN,
    decode_script_num,
    encode_script_num,
)
from pyzcash.script.opcodes import Opcode
from pyzcash.script.script import (
    MAX_SCRIPT_ELEMENT_SIZE,
    MAX_SCRIPT_SIZE,
    Operation,
    Script,
    ScriptItem,
)
from pyzcash.script.standard import (
    MAX_MULTISIG_KEYS,
    address_from_script_pubkey,
    multisig_script,
    op_return_script,
    p2pkh_script_pubkey,
    p2sh_script_pubkey,
    script_pubkey_for,
)

__all__ = [
    "DEFAULT_MAX_LEN",
    "MAX_MULTISIG_KEYS",
    "MAX_SCRIPT_ELEMENT_SIZE",
    "MAX_SCRIPT_SIZE",
    "Opcode",
    "Operation",
    "Script",
    "ScriptItem",
    "address_from_script_pubkey",
    "decode_script_num",
    "encode_script_num",
    "multisig_script",
    "op_return_script",
    "p2pkh_script_pubkey",
    "p2sh_script_pubkey",
    "script_pubkey_for",
]
