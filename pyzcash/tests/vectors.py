"""Real Zcash encodings used as test vectors.

These are regtest viewing keys lifted from the integration suite's test-wallet
manifests (qa/rpc-tests/test-wallet/manifests/), which the test framework's own
ufvk_decode selftest checks. Using them means the extracted encoding layer is
verified against strings a real wallet produced, not against values invented
alongside the code they are meant to test.
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
