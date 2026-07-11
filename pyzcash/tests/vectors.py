"""Real Zcash encodings used as test vectors.

These are regtest keys and addresses lifted from the integration suite's
test-wallet fixtures (qa/rpc-tests/test-wallet/), some of which the test
framework's own ufvk_decode selftest checks. Using them means the extracted
library is verified against strings a real wallet produced, not against values
invented alongside the code they are meant to test.

This module holds the raw data. conftest.py exposes it as fixtures; the values
are also importable directly, because pytest's parametrize cannot consume a
fixture.
"""

from __future__ import annotations

# A Sapling extended full viewing key: Bech32 (not Bech32m), 169-byte payload.
SAPLING_EXTFVK = (
    "zxviewregtestsapling1qv8mp7xgpqqqpq93lgz93td8ruf9wlshqvvhvhmuk25tu7qpzc3u"
    "85kml7t6se722c9mpurn6jxjl3frgpqp9hs0wmcp5z0s022x49lx45tz7g652gqz3hnx8xftv"
    "mx5le47xcsg67k5senree9v90wjd05plssmh3kaws9pw8jwr7eea6j3zdwjszms9ffef6d2vl"
    "cr7u9xj6ttg7vemnyst9tu3nlnp8p08nlhghppv0ml4nsf87xgavyu74hz45656gxw3khqlqg"
    "axldea"
)
SAPLING_EXTFVK_PAYLOAD_LEN = 169

# A unified full viewing key: Bech32m, F4Jumbled, carrying Sapling + Orchard.
UNIFIED_FVK = (
    "uviewregtest19v4qrwsvdw6fl6njyd9esayxvwpts9qrkq5y9hpunqy4qy0r3rmvw7hkdn8k"
    "m3azhr68lwfvp99t8a6x0akkm4lg3eygrduj33l93cr5l7wavagknudjh5ae35j8rj4eejmet"
    "k02jd8c8pe6q3e3gvxy7jgfxz67n2ka5s9pn0zwshhdzusqps32tafm8z4hwk49lhgzd0crmm"
    "8ftg9agta2f0vd4jk7su87p9f6xtfq9xpdqz9xpjsnerkuqptfenjs646r3r9h3tqyhw7ezn4"
    "lfwce4vsv08qsxxjy2y9zjgc35mt9mafrlym4wafp2huvrz0dg7hnjtzfas7g9f20mmfz7phu"
    "8lwh0yawycp02sw0hdzgn7q6wwjvkc3cnkpsj37999rway7ecc23ajeuyj97949mhv08akf62"
    "8lq9qql7t2uhlq4p50p5uvehaggw3p3428ds8t3jdpf4dmgj0n85hkyjmutmg3njcw4v3dtms"
    "ea3xdj"
)
UNIFIED_FVK_HRP = "uviewregtest"

# ZIP 316 receiver typecodes.
TYPECODE_P2PKH = 0x00
TYPECODE_SAPLING = 0x02
TYPECODE_ORCHARD = 0x03

# The Sapling and Orchard lengths are the ones the test framework's ufvk_decode
# selftest asserts. The key also carries a transparent P2PKH viewing key (a
# 32-byte chain code and a 33-byte compressed public key), which that selftest
# never looked at because it only ever asked for the two shielded receivers.
TRANSPARENT_FVK_LEN = 65
SAPLING_FVK_LEN = 128
ORCHARD_FVK_LEN = 96


# --- addresses (regtest, from qa/rpc-tests/test-wallet/) ---------------------

P2SH_ADDRESS = "t2AELYrVCe7Cy2tdFz8XfWpG42ps3zJm39K"

SAPLING_ADDRESS = (
    "zregtestsapling1235ejypsw2f0mwesy0ygtp4tknrm2znypkurm8wac25yywgdfzrgcr0dre"
    "udf96whh47s52fz6n"
)

UNIFIED_ADDRESS = (
    "uregtest10l3s7t08y44grzpr462x6kmvt3g4nsd8gmy69ags72c9hdr28t62k3fg9e5x8ayfu"
    "5cg2rvwk4ytm06fqvs092zeytx9f5kcer38lztz50janztazqxq0vn5jxll8tjqshsvexqdxpw"
    "2548x9j6y0cm2vw9dud30khupf57gj7tk6eyrxxf7ycvaddv9gyrjal0s8dpnafl7qghqat7"
)

ALL_ADDRESSES = (P2SH_ADDRESS, SAPLING_ADDRESS, UNIFIED_ADDRESS)

# Base58 payloads worth exercising. The leading-zero cases matter because they
# encode as leading '1' characters, which is the classic place a Base58
# implementation goes wrong; the 22-byte case is the shape of a real
# transparent address (a 2-byte version prefix and a 20-byte hash).
BASE58_PAYLOADS = (
    b"",
    b"\x00",
    b"\x00\x00\x00",
    b"\x00\x01\x02",
    bytes(range(22)),
    b"\x1c\xb8" + bytes(range(20)),
    b"\xff" * 32,
)
