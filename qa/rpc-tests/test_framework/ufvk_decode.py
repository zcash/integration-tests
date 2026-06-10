#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

"""
Pure-Python helpers for matching Zcash viewing-key material against rows
in `wallet.db`. Used by `zcashd_key_import.py` to assert that imported
keys land in the wallet without going through the JSON-RPC.

Limited to operations that don't need elliptic-curve arithmetic:
  * bech32m decode (BIP-350)
  * sapling extended FVK (`zxview...`) -> 128-byte dfvk slice
  * unified address / unified FVK (`u...`, `uview...`) -> typed-receiver split
  * compactsize varint decode (used by typed-receiver TLV bodies)

Computing a sapling/orchard FVK from a SPENDING key (`secret-extended-key...`)
is out of scope: that requires Jubjub scalar multiplication.
"""

import hashlib

from embit.bech32 import CHARSET, bech32_hrp_expand, bech32_polymod, convertbits

# Zcash uses bech32 (constant 1) for legacy encodings (sapling extended
# spending key / extended FVK; sapling addresses on regtest), and bech32m
# (constant 0x2bc830a3) for unified containers (UA / UFVK / UIVK). embit's
# own bech32_decode rejects long-form encodings (UFVKs exceed BIP-173's
# 90-char cap) and returns None rather than raising, so we reuse its
# primitives and wrap our own checksum / length handling.
_BECH32_CONST = 1
_BECH32M_CONST = 0x2bc830a3


def bech32_decode(s):
    """
    Decode a bech32 or bech32m string, accepting either checksum constant.

    Returns (hrp, data_bytes, encoding) where encoding is "bech32" or
    "bech32m".

    Raises ValueError if:
      * a character is outside the printable range (ord < 33 or > 126);
      * the string mixes upper- and lower-case (forbidden by BIP-173);
      * the "1" separator is missing or too close to either end;
      * a data character is not in the bech32 CHARSET;
      * the checksum matches neither the bech32 nor the bech32m constant;
      * the 5-to-8-bit repacking has invalid (non-zero) padding.
    """
    s = s.strip()
    if any(ord(c) < 33 or ord(c) > 126 for c in s):
        raise ValueError("bech32: bad character range")
    # BIP-173: mixed-case strings MUST be rejected.
    if any(c.islower() for c in s) and any(c.isupper() for c in s):
        raise ValueError("bech32: mixed-case strings are forbidden")
    s = s.lower()
    pos = s.rfind('1')
    if pos < 1 or pos + 7 > len(s):
        raise ValueError("bech32: bad separator position")
    hrp = s[:pos]
    data = []
    for c in s[pos + 1:]:
        if c not in CHARSET:
            raise ValueError(f"bech32: invalid char {c!r}")
        data.append(CHARSET.index(c))
    chk = bech32_polymod(bech32_hrp_expand(hrp) + data)
    if chk == _BECH32M_CONST:
        encoding = "bech32m"
    elif chk == _BECH32_CONST:
        encoding = "bech32"
    else:
        raise ValueError("bech32: checksum failed")
    payload = convertbits(data[:-6], 5, 8, pad=False)
    if payload is None:
        raise ValueError("bech32: invalid padding")
    return hrp, bytes(payload), encoding


# ---------------------------------------------------------------------------
# Sapling extended full viewing key
# ---------------------------------------------------------------------------

# Layout: depth(1) | parent_fvk_tag(4) | child_index(4) | chain_code(32) |
#         ak(32) | nk(32) | ovk(32) | dk(32)
# = 1 + 4 + 4 + 32 + 128 = 169 bytes total.
# The trailing 128 bytes (ak||nk||ovk||dk) are the dfvk format used by
# zallet's `ext_zallet_keystore_standalone_sapling_keys.dfvk` column.
_SAPLING_EXTFVK_LEN = 169
_SAPLING_DFVK_OFFSET = 41


def sapling_dfvk_from_extfvk(encoded):
    """
    Given an encoded sapling extended FVK (e.g. `zxviewregtestsapling1...`,
    bech32-encoded per ZIP-32), return the 128-byte dfvk (ak||nk||ovk||dk)
    usable for matching against the keystore table.

    Raises ValueError if `encoded` fails to decode (see `bech32_decode`),
    its HRP does not start with "zxview", or its payload is not the
    expected 169 bytes.
    """
    hrp, raw, _enc = bech32_decode(encoded)
    if not hrp.startswith("zxview"):
        raise ValueError(f"sapling extfvk: unexpected hrp {hrp!r}")
    if len(raw) != _SAPLING_EXTFVK_LEN:
        raise ValueError(
            f"sapling extfvk: expected {_SAPLING_EXTFVK_LEN} bytes, got {len(raw)}")
    return raw[_SAPLING_DFVK_OFFSET:]


# ---------------------------------------------------------------------------
# Unified addresses / unified FVKs (ZIP-316)
# ---------------------------------------------------------------------------

# Typecodes per ZIP-316
TYPECODE_P2PKH = 0x00
TYPECODE_P2SH = 0x01
TYPECODE_SAPLING = 0x02
TYPECODE_ORCHARD = 0x03

# F4Jumble/padding pad length is 16 bytes ("u" + hrp padded to 16 with zeros).
_F4JUMBLE_PAD_LEN = 16


def _read_compactsize(buf, offset):
    """Decode a Bitcoin-style compactsize at offset. Returns (value, new_offset)."""
    b0 = buf[offset]
    if b0 < 0xfd:
        return b0, offset + 1
    if b0 == 0xfd:
        return int.from_bytes(buf[offset + 1:offset + 3], 'little'), offset + 3
    if b0 == 0xfe:
        return int.from_bytes(buf[offset + 1:offset + 5], 'little'), offset + 5
    return int.from_bytes(buf[offset + 1:offset + 9], 'little'), offset + 9


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


# F4Jumble (ZIP-316 section F4Jumble) - recover the cleartext payload
# (receivers || 16-byte HRP padding) from the bech32m-decoded wire bytes of
# a unified container.
#
# Forward F4Jumble per the librustzcash reference (and verified by hand on a
# regtest UA), with l_L = ceil(L/2) when L <= 128 else 64, and l_R = L - l_L:
#
#     R ^= G_0(L)
#     L ^= H_0(R)
#     R ^= G_1(L)
#     L ^= H_1(R)
#
# G_i takes the L half as input and produces an l_R-byte output that is XORed
# into R. H_i takes R and produces an l_L-byte output XORed into L. The
# inverse undoes the four mixings in reverse order (H_1, G_1, H_0, G_0).
def _F4Jumble_inverse(message):
    # Per ZIP-316 section F4Jumble (https://zips.z.cash/zip-0316):
    # 48 <= L <= 2^22 + 64, and l_L = ceil(L/2) when L <= 128 else 64.
    total_len = len(message)
    if total_len < 48 or total_len > 4194368:
        raise ValueError("F4Jumble: invalid message length")
    l_L = (total_len + 1) // 2 if total_len <= 128 else 64
    l_R = total_len - l_L

    def H(i, u):
        return hashlib.blake2b(
            u, digest_size=l_L,
            person=b'UA_F4Jumble_H' + bytes([i]) + b'\x00\x00').digest()

    def G(i, u):
        out = b''
        j = 0
        while len(out) < l_R:
            out += hashlib.blake2b(
                u, digest_size=64,
                person=b'UA_F4Jumble_G' + bytes([i]) + j.to_bytes(2, 'little')).digest()
            j += 1
        return out[:l_R]

    def xor(p, q):
        return bytes(x ^ y for x, y in zip(p, q))

    L = message[:l_L]
    R = message[l_L:]
    L = xor(L, H(1, R))   # undo the L ^= H_1(R) round
    R = xor(R, G(1, L))   # undo the R ^= G_1(L) round
    L = xor(L, H(0, R))   # undo the L ^= H_0(R) round
    R = xor(R, G(0, L))   # undo the R ^= G_0(L) round
    return L + R


def split_unified_receivers(encoded):
    """
    Decode a unified-container encoding (`u...`, `uregtest...`, `uview...`,
    `uviewregtest...`, bech32m per ZIP-316) and return `{typecode: data_bytes}`
    for each receiver.

    Only typecodes 0x00..0x03 are recognized; anything else is included as a
    raw bytes blob keyed by its integer typecode.

    Raises ValueError if `encoded` fails to decode (see `bech32_decode`), is
    not bech32m, has a padding mismatch, or declares a receiver length that
    runs past the end of the payload.
    """
    hrp, raw, encoding = bech32_decode(encoded)
    if encoding != "bech32m":
        raise ValueError(f"unified container expected bech32m, got {encoding}")
    # Inverse-jumble to recover cleartext, then strip the trailing padding.
    cleartext = _F4Jumble_inverse(raw)
    body = _strip_padding(cleartext, hrp)

    receivers = {}
    off = 0
    while off < len(body):
        tc, off = _read_compactsize(body, off)
        ln, off = _read_compactsize(body, off)
        if off + ln > len(body):
            raise ValueError(
                f"unified: receiver tc={tc} runs past end (need {ln} have {len(body)-off})")
        receivers[tc] = bytes(body[off:off + ln])
        off += ln
    return receivers


def sapling_dfvk_from_unified_fvk(encoded):
    """
    Extract the 128-byte sapling dfvk (ak||nk||ovk||dk) from a unified FVK
    (`uview...` / `uviewregtest...`) if it carries a sapling receiver, else
    None.

    Raises ValueError if `encoded` fails to decode (see
    `split_unified_receivers`).
    """
    receivers = split_unified_receivers(encoded)
    return receivers.get(TYPECODE_SAPLING)


def orchard_fvk_from_unified_fvk(encoded):
    """
    Extract the 96-byte orchard FVK from a unified FVK if present, else None.

    Raises ValueError if `encoded` fails to decode (see
    `split_unified_receivers`).
    """
    receivers = split_unified_receivers(encoded)
    return receivers.get(TYPECODE_ORCHARD)


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
