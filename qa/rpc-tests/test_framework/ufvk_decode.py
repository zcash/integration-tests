#!/usr/bin/env python3
# Copyright (c) 2025 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

"""
Decoding helpers for Sapling extended full viewing keys and ZIP 316 unified
containers (unified addresses, UFVKs, UIVKs).

The decoding is now done by pyzcash. This module keeps the names the tests
already import.

Two things changed by delegating, beyond removing the duplicated code:

- The `embit` dependency is gone. It was pulled in only for four Bech32
  primitives, because embit's own decoder rejects strings longer than 90
  characters, which is a BIP 173 rule that ZIP 316 explicitly lifts: a unified
  container carrying several receivers is longer than that. pyzcash implements
  Bech32 and Bech32m with no length cap, which is what Zcash requires.

- A real bug is fixed. This module's F4Jumble split the message at
  `ceil(len / 2)`, where ZIP 316 specifies `min(floor(len / 2), 64)`. The two
  differ only for an odd length below 128, and the wrong split is still a
  permutation, so it inverted cleanly and every round trip passed. It would
  silently mis-decode any unified container with an odd-length payload, which
  includes a unified address whose only receiver is Sapling.
"""

from pyzcash.encoding import Bech32Encoding, Reader
from pyzcash.encoding import bech32_decode as _bech32_decode
from pyzcash.encoding import f4jumble_inverse

# Typecodes per ZIP-316
TYPECODE_P2PKH = 0x00
TYPECODE_P2SH = 0x01
TYPECODE_SAPLING = 0x02
TYPECODE_ORCHARD = 0x03

# F4Jumble/padding pad length is 16 bytes ("u" + hrp padded to 16 with zeros).
_F4JUMBLE_PAD_LEN = 16

# A Sapling extended full viewing key is 169 bytes; the diversifiable full
# viewing key (ak || nk || ovk || dk) is the 128 bytes at offset 41.
_SAPLING_EXTFVK_LEN = 169
_SAPLING_DFVK_OFFSET = 41
_SAPLING_DFVK_LEN = 128


def bech32_decode(s):
    """
    Decode a Bech32 or Bech32m string, returning `(hrp, data_bytes, encoding)`
    where `encoding` is the string 'bech32' or 'bech32m'.

    Raises ValueError on a malformed string or a bad checksum, as before.
    """
    hrp, payload, encoding = _bech32_decode(s)
    name = "bech32" if encoding is Bech32Encoding.BECH32 else "bech32m"
    return (hrp, payload, name)


def sapling_dfvk_from_extfvk(encoded):
    """
    Decode a ZIP-32 Sapling extended full viewing key (`zxview...`) and return
    its 128-byte diversifiable full viewing key (ak || nk || ovk || dk).
    """
    (hrp, raw, encoding) = bech32_decode(encoded)
    if not hrp.startswith("zxview"):
        raise ValueError("not a Sapling extended full viewing key: %s" % (hrp,))
    if encoding != "bech32":
        raise ValueError("a Sapling extended FVK is bech32, not bech32m")
    if len(raw) != _SAPLING_EXTFVK_LEN:
        raise ValueError(
            "Sapling extended FVK: expected %d bytes, got %d"
            % (_SAPLING_EXTFVK_LEN, len(raw))
        )
    return raw[_SAPLING_DFVK_OFFSET:_SAPLING_DFVK_OFFSET + _SAPLING_DFVK_LEN]


def _strip_padding(payload, hrp):
    """
    Strip the trailing 16-byte ZIP-316 padding (`hrp` zero-padded to 16).
    Returns the receiver list bytes.
    """
    if len(payload) < _F4JUMBLE_PAD_LEN:
        raise ValueError("unified payload: too short for padding")
    pad = payload[-_F4JUMBLE_PAD_LEN:]
    expected = hrp.encode() + b'\x00' * (_F4JUMBLE_PAD_LEN - len(hrp))
    if pad != expected:
        raise ValueError("unified payload: padding mismatch")
    return payload[:-_F4JUMBLE_PAD_LEN]


def split_unified_receivers(encoded):
    """
    Decode a unified-container encoding (`u...`, `uregtest...`, `uview...`,
    `uviewregtest...`, bech32m per ZIP-316) and return `{typecode: data_bytes}`
    for each receiver.

    Only typecodes 0x00..0x03 are recognized; anything else is included as a
    raw bytes blob keyed by its integer typecode.
    """
    (hrp, payload, encoding) = bech32_decode(encoded)
    if encoding != "bech32m":
        raise ValueError("a unified container is bech32m, not bech32")

    plaintext = _strip_padding(f4jumble_inverse(payload), hrp)

    receivers = {}
    reader = Reader(plaintext)
    while reader.remaining:
        typecode = reader.read_compact_size()
        receivers[typecode] = reader.read_bytes_compact()
    return receivers


def sapling_dfvk_from_unified_fvk(encoded):
    """
    Return the 128-byte Sapling dfvk carried by a UFVK, or None if it has none.
    """
    return split_unified_receivers(encoded).get(TYPECODE_SAPLING)


def orchard_fvk_from_unified_fvk(encoded):
    """
    Return the 96-byte Orchard fvk carried by a UFVK, or None if it has none.
    """
    return split_unified_receivers(encoded).get(TYPECODE_ORCHARD)


def selftest():
    # Run with `python3 ufvk_decode.py`, or call from a test that already
    # depends on this module (e.g. `zcashd_key_import_db.py`) so it also
    # runs in CI. Vectors are pulled from the test-wallet phase manifests
    # so the test follows the fixture if it is regenerated. Kept in a
    # function so its locals don't leak into the module namespace when this
    # file is imported as a library.
    import json
    import os

    manifest_dir = os.path.join(
        os.path.dirname(os.path.realpath(__file__)),
        "..", "test-wallet", "manifests")
    extfvk = ufvk = None
    for name in sorted(os.listdir(manifest_dir)):
        if not (name.startswith("phase_") and name.endswith(".json")):
            continue
        with open(os.path.join(manifest_dir, name), encoding="utf-8") as f:
            manifest = json.load(f)
        for entry in manifest.get("viewing_keys", {}).get("sapling", []):
            if extfvk is None and entry.get("viewing_key", "").startswith("zxview"):
                extfvk = entry["viewing_key"]
        for entry in manifest.get("viewing_keys", {}).get("orchard", []):
            if ufvk is None and entry.get("viewing_key", "").startswith("uviewregtest1"):
                ufvk = entry["viewing_key"]
        if extfvk and ufvk:
            break
    assert extfvk and ufvk, "fixture missing zxview*/uviewregtest1* viewing keys"

    hrp, raw, enc = bech32_decode(extfvk)
    assert hrp.startswith("zxview") and enc == "bech32" and len(raw) == 169

    dfvk = sapling_dfvk_from_extfvk(extfvk)
    assert isinstance(dfvk, bytes) and len(dfvk) == 128

    receivers = split_unified_receivers(ufvk)
    assert TYPECODE_SAPLING in receivers and len(receivers[TYPECODE_SAPLING]) == 128
    assert TYPECODE_ORCHARD in receivers and len(receivers[TYPECODE_ORCHARD]) == 96
    assert sapling_dfvk_from_unified_fvk(ufvk) == receivers[TYPECODE_SAPLING]
    assert orchard_fvk_from_unified_fvk(ufvk) == receivers[TYPECODE_ORCHARD]
    print("OK")


if __name__ == "__main__":
    selftest()
