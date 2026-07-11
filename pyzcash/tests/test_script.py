"""Scripts, checked against real ones the node itself decoded.

The vectors come from qa/rpc-tests/decodescript.py, whose assertions are the
node's own decodescript output. If this parser and that test disagree about what
a script says, one of them is wrong about a real transaction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pyzcash.address import TransparentAddress, TransparentKind
from pyzcash.encoding import hash160
from pyzcash.errors import ParseError
from pyzcash.script import (
    Opcode,
    Script,
    address_from_script_pubkey,
    decode_script_num,
    encode_script_num,
    multisig_script,
    op_return_script,
    p2pkh_script_pubkey,
    p2sh_script_pubkey,
    script_pubkey_for,
)
from tests.vectors import (
    MULTISIG_2OF3_HEX,
    OP_RETURN_DATA,
    OP_RETURN_SCRIPT_HEX,
    P2PKH_SCRIPT_HASH,
    P2PKH_SCRIPT_HEX,
    P2SH_SCRIPT_HASH,
    P2SH_SCRIPT_HEX,
    PUSHDATA1_SCRIPT_HEX,
)

if TYPE_CHECKING:
    from pyzcash import Network

# --- the standard templates, against the node's own scripts ------------------


def test_p2pkh_template_reproduces_a_real_script() -> None:
    assert p2pkh_script_pubkey(P2PKH_SCRIPT_HASH).to_hex() == P2PKH_SCRIPT_HEX


def test_p2sh_template_reproduces_a_real_script() -> None:
    assert p2sh_script_pubkey(P2SH_SCRIPT_HASH).to_hex() == P2SH_SCRIPT_HEX


def test_op_return_template() -> None:
    assert (
        op_return_script(OP_RETURN_DATA).to_hex()
        == "6a09" + OP_RETURN_DATA.hex()
    )
    assert op_return_script().to_hex() == "6a"


def test_a_real_op_return_script_is_not_just_a_bare_push() -> None:
    """The real script carries a trailing opcode after its data push.

    It is OP_RETURN, a 9-byte push, and then 0x80 (OP_LEFT, a disabled opcode).
    Nothing forbids that: the output is unspendable either way. It is kept as a
    vector precisely because it is not the tidy template, and a parser that
    assumed OP_RETURN outputs are always "OP_RETURN <push>" would misread it.
    """
    ops = Script.from_hex(OP_RETURN_SCRIPT_HEX).operations()
    assert [op.opcode for op in ops] == [
        Opcode.OP_RETURN,
        len(OP_RETURN_DATA),
        Opcode.OP_LEFT,
    ]
    assert ops[1].data == OP_RETURN_DATA


def test_a_real_p2pkh_script_parses_as_the_node_reports_it() -> None:
    """The node renders this as:

    OP_DUP OP_HASH160 <hash> OP_EQUALVERIFY OP_CHECKSIG
    """
    ops = Script.from_hex(P2PKH_SCRIPT_HEX).operations()
    assert [op.opcode for op in ops] == [
        Opcode.OP_DUP,
        Opcode.OP_HASH160,
        len(P2PKH_SCRIPT_HASH),  # a direct push of 20 bytes
        Opcode.OP_EQUALVERIFY,
        Opcode.OP_CHECKSIG,
    ]
    assert ops[2].data == P2PKH_SCRIPT_HASH


def test_a_real_multisig_redeem_script_parses() -> None:
    ops = Script.from_hex(MULTISIG_2OF3_HEX).operations()
    assert ops[0].opcode == Opcode.OP_2
    assert ops[-2].opcode == Opcode.OP_3
    assert ops[-1].opcode == Opcode.OP_CHECKMULTISIG
    pubkeys = [op.data for op in ops if op.is_push]
    assert len(pubkeys) == 3
    assert all(key is not None and len(key) == 33 for key in pubkeys)


def test_pushdata1_is_parsed() -> None:
    """The real P2SH scriptSig pushes its 105-byte redeem script with PUSHDATA1.

    A parser that only handles direct pushes (lengths up to 0x4b) silently
    misreads this, which is why it is worth a vector of its own.
    """
    ops = Script.from_hex(PUSHDATA1_SCRIPT_HEX).operations()
    assert len(ops) == 1
    assert ops[0].opcode == Opcode.OP_PUSHDATA1
    assert ops[0].data == bytes.fromhex(MULTISIG_2OF3_HEX)


def test_multisig_template_reproduces_the_real_redeem_script() -> None:
    ops = Script.from_hex(MULTISIG_2OF3_HEX).operations()
    pubkeys = [op.data for op in ops if op.is_push and op.data is not None]
    assert multisig_script(2, pubkeys).to_hex() == MULTISIG_2OF3_HEX


# --- scripts and addresses --------------------------------------------------


@pytest.mark.parametrize("kind", [TransparentKind.P2PKH, TransparentKind.P2SH])
def test_address_to_script_and_back(
    kind: TransparentKind, mainnet: Network
) -> None:
    address = TransparentAddress(
        network=mainnet, kind=kind, hash=bytes(range(20))
    )
    script = script_pubkey_for(address)
    assert address_from_script_pubkey(script, mainnet) == address


def test_a_script_paying_a_real_address_round_trips(mainnet: Network) -> None:
    script = Script.from_hex(P2PKH_SCRIPT_HEX)
    address = address_from_script_pubkey(script, mainnet)
    assert address is not None
    assert address.kind is TransparentKind.P2PKH
    assert script_pubkey_for(address).to_hex() == P2PKH_SCRIPT_HEX


def test_a_p2sh_script_decodes_to_a_p2sh_address(mainnet: Network) -> None:
    address = address_from_script_pubkey(
        Script.from_hex(P2SH_SCRIPT_HEX), mainnet
    )
    assert address is not None
    assert address.kind is TransparentKind.P2SH
    assert address.hash == P2SH_SCRIPT_HASH


@pytest.mark.parametrize(
    "hex_script", [OP_RETURN_SCRIPT_HEX, MULTISIG_2OF3_HEX, ""]
)
def test_a_non_standard_script_has_no_address(
    hex_script: str, mainnet: Network
) -> None:
    """None means "pays to no address", a normal thing for an output to be."""
    assert (
        address_from_script_pubkey(Script.from_hex(hex_script), mainnet) is None
    )


def test_hash160_of_a_redeem_script_is_its_p2sh_hash(mainnet: Network) -> None:
    """This is the relationship P2SH is built on."""
    redeem = Script.from_hex(MULTISIG_2OF3_HEX)
    script_pubkey = p2sh_script_pubkey(hash160(redeem.raw))
    address = address_from_script_pubkey(script_pubkey, mainnet)
    assert address is not None
    assert address.kind is TransparentKind.P2SH


# --- parsing failures -------------------------------------------------------


def test_a_push_running_past_the_end_is_an_error() -> None:
    with pytest.raises(ParseError, match="runs past the end"):
        Script(bytes([0x05, 0x01, 0x02])).operations()  # promises 5, has 2


def test_a_truncated_pushdata1_length_is_an_error() -> None:
    with pytest.raises(ParseError, match="truncated"):
        Script(bytes([Opcode.OP_PUSHDATA1])).operations()


def test_a_pushdata1_claiming_more_than_it_has_is_an_error() -> None:
    with pytest.raises(ParseError, match="claims 200 bytes"):
        Script(bytes([Opcode.OP_PUSHDATA1, 200, 0x01])).operations()


def test_an_unparseable_script_still_reprs() -> None:
    """A repr that raised would make debugging a malformed script impossible."""
    assert "unparseable" in repr(Script(bytes([0x05, 0x01])))


# --- script numbers ---------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "encoded"),
    [
        (0, ""),
        (1, "01"),
        (-1, "81"),
        (127, "7f"),
        (128, "8000"),  # 0x80 alone would read as -0, so a byte is added
        (-128, "8080"),
        (255, "ff00"),
        (256, "0001"),
        (-256, "0081"),
        (32767, "ff7f"),
        (32768, "008000"),
    ],
)
def test_script_num_encoding(value: int, encoded: str) -> None:
    assert encode_script_num(value).hex() == encoded
    assert decode_script_num(bytes.fromhex(encoded)) == value


@pytest.mark.parametrize(
    "value", [0, 1, -1, 2, -2, 127, -127, 128, -128, 32767, -32768, 2**31 - 1]
)
def test_script_num_round_trips(value: int) -> None:
    assert decode_script_num(encode_script_num(value), max_len=5) == value


@pytest.mark.parametrize("encoded", ["00", "0000", "80", "0100"])
def test_non_minimal_script_numbers_are_rejected(encoded: str) -> None:
    """A number with two encodings would give a script two hashes."""
    with pytest.raises(ParseError, match="not minimally encoded"):
        decode_script_num(bytes.fromhex(encoded))


def test_an_over_long_script_number_is_rejected() -> None:
    with pytest.raises(ParseError, match="at most 4"):
        decode_script_num(bytes.fromhex("0102030405"))


# --- building ---------------------------------------------------------------


def test_build_distinguishes_an_opcode_from_a_number() -> None:
    """Opcode.OP_1 emits the opcode; the int 1 pushes the number 1."""
    assert Script.build(Opcode.OP_1).raw == bytes([0x51])
    assert Script.build(1).raw == bytes([0x01, 0x01])


def test_build_chooses_the_shortest_push() -> None:
    assert Script.build(bytes(1)).raw[0] == 1  # direct push
    assert Script.build(bytes(76)).raw[0] == Opcode.OP_PUSHDATA1
    assert Script.build(bytes(256)).raw[0] == Opcode.OP_PUSHDATA2


def test_an_empty_script_is_falsy() -> None:
    assert not Script()
    assert len(Script()) == 0
