"""The canonical zcash-test-vectors corpus, applied to the encoding and address
layers.

These are the vectors librustzcash, the sapling and orchard crates, and zcashd
test against. Agreeing with them is the strongest evidence available that this
implementation reads Zcash correctly, rather than merely consistently with
itself. The transaction corpus is exercised in test_transaction.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pyzcash import Network
from pyzcash.address import (
    Receiver,
    ReceiverType,
    TexAddress,
    TransparentAddress,
    TransparentKind,
    UnifiedAddress,
    UnknownReceiver,
    parse_address,
)
from pyzcash.encoding import bech32_decode, f4jumble, f4jumble_inverse
from tests.json_vectors import load_vectors, pinned_commit, provenance


def _payload_hex(vector: dict[str, object]) -> str:
    """The F4Jumbled payload a unified address carries, before unjumbling."""
    return bech32_decode(str(vector["unified_addr"]))[1].hex()


if TYPE_CHECKING:
    from collections.abc import Sequence

F4JUMBLE = load_vectors("f4jumble")
UNIFIED_ADDRESSES = load_vectors("unified_address")
TEX_ADDRESSES = load_vectors("zip_0320")


def test_the_vectors_are_pinned_and_attributed() -> None:
    """A vendored vector without provenance is just a magic number."""
    assert len(pinned_commit()) == 40
    assert "zcash-test-vectors" in provenance("f4jumble")


# --- F4Jumble (ZIP 316) -----------------------------------------------------


@pytest.mark.parametrize("vector", F4JUMBLE, ids=range(len(F4JUMBLE)))
def test_f4jumble_matches_the_canonical_vectors(
    vector: dict[str, object],
) -> None:
    normal = bytes.fromhex(str(vector["normal"]))
    jumbled = bytes.fromhex(str(vector["jumbled"]))
    assert f4jumble(normal) == jumbled
    assert f4jumble_inverse(jumbled) == normal


def test_f4jumble_splits_odd_lengths_below_128_correctly() -> None:
    """Regression: the left half is floor(len/2), not ceil(len/2).

    The two differ only for an odd length below 128, and getting it wrong
    produces a different but still perfectly invertible permutation. That is
    what makes this bug so quiet: a round-trip test cannot catch it, because
    the wrong permutation round-trips too. Nor do the canonical f4jumble
    vectors, none of which is an odd length below 128.

    Only a cross-implementation vector catches it. A unified address carrying a
    single Sapling receiver has a 61-byte payload, so the address vectors do,
    and they are what found this: the test framework this code was extracted
    from used the ceiling and had the bug.
    """
    odd = [
        v
        for v in UNIFIED_ADDRESSES
        if len(bytes.fromhex(_payload_hex(v))) % 2 == 1
        and len(bytes.fromhex(_payload_hex(v))) < 128
    ]
    assert odd, "no odd-length-below-128 unified address to pin the bug with"
    for vector in odd:
        UnifiedAddress.decode(str(vector["unified_addr"]), Network.MAIN)


# --- unified addresses (ZIP 316) --------------------------------------------


def _receivers_of(
    vector: dict[str, object],
) -> list[Receiver | UnknownReceiver]:
    """The receiver list a vector describes, in ascending typecode order."""
    receivers: list[Receiver | UnknownReceiver] = []
    for column, receiver_type in (
        ("p2pkh_bytes", ReceiverType.P2PKH),
        ("p2sh_bytes", ReceiverType.P2SH),
        ("sapling_raw_addr", ReceiverType.SAPLING),
        ("orchard_raw_addr", ReceiverType.ORCHARD),
    ):
        value = vector[column]
        if value is not None:
            receivers.append(
                Receiver(type=receiver_type, data=bytes.fromhex(str(value)))
            )

    typecode = vector["unknown_typecode"]
    if typecode is not None:
        receivers.append(
            UnknownReceiver(
                typecode=int(str(typecode)),
                data=bytes.fromhex(str(vector["unknown_bytes"])),
            )
        )
    receivers.sort(key=lambda r: r.typecode)
    return receivers


@pytest.mark.parametrize(
    "vector", UNIFIED_ADDRESSES, ids=range(len(UNIFIED_ADDRESSES))
)
def test_unified_addresses_decode_to_the_receivers_the_vector_names(
    vector: dict[str, object],
) -> None:
    encoded = str(vector["unified_addr"])
    address = UnifiedAddress.decode(encoded, Network.MAIN)
    assert list(address.receivers) == _receivers_of(vector)


@pytest.mark.parametrize(
    "vector", UNIFIED_ADDRESSES, ids=range(len(UNIFIED_ADDRESSES))
)
def test_unified_addresses_re_encode_to_the_canonical_string(
    vector: dict[str, object],
) -> None:
    """Building from the receivers must reproduce the exact string."""
    address = UnifiedAddress(
        network=Network.MAIN, receivers=tuple(_receivers_of(vector))
    )
    assert address.encode() == str(vector["unified_addr"])


def test_the_corpus_covers_unknown_receiver_typecodes() -> None:
    """The forward-compatibility path is exercised, not just asserted about.

    A receiver of a type this library does not know must survive a decode and a
    re-encode untouched, or an old wallet would silently rewrite an address into
    a form its owner never chose.
    """
    with_unknown = [
        v for v in UNIFIED_ADDRESSES if v["unknown_typecode"] is not None
    ]
    assert with_unknown, "no unknown-typecode vectors to cover the path"

    address = UnifiedAddress.decode(
        str(with_unknown[0]["unified_addr"]), Network.MAIN
    )
    unknown = [r for r in address.receivers if isinstance(r, UnknownReceiver)]
    assert unknown
    assert address.encode() == str(with_unknown[0]["unified_addr"])


# --- TEX addresses (ZIP 320) ------------------------------------------------


@pytest.mark.parametrize("vector", TEX_ADDRESSES, ids=range(len(TEX_ADDRESSES)))
def test_tex_addresses_match_the_canonical_vectors(
    vector: dict[str, object],
) -> None:
    pubkey_hash = bytes.fromhex(str(vector["p2pkh_bytes"]))

    tex = TexAddress(network=Network.MAIN, hash=pubkey_hash)
    assert tex.encode() == str(vector["tex_addr"])
    assert TexAddress.decode(str(vector["tex_addr"]), Network.MAIN) == tex

    # The same hash, as an ordinary transparent address.
    transparent = TransparentAddress(
        network=Network.MAIN, kind=TransparentKind.P2PKH, hash=pubkey_hash
    )
    assert transparent.encode() == str(vector["t_addr"])
    assert parse_address(str(vector["t_addr"]), Network.MAIN) == transparent

    # ZIP 320's whole point: a TEX address names the same P2PKH recipient.
    assert tex.to_transparent() == transparent


def test_every_vector_set_is_non_empty() -> None:
    """A silently empty vector file would make every test above vacuous."""
    sets: Sequence[tuple[str, Sequence[object]]] = [
        ("f4jumble", F4JUMBLE),
        ("unified_address", UNIFIED_ADDRESSES),
        ("zip_0320", TEX_ADDRESSES),
    ]
    for name, vectors in sets:
        assert len(vectors) > 1, f"{name} has no vectors"
