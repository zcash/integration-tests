"""The hash functions Zcash's encodings and digests are built from."""

from __future__ import annotations

import hashlib

__all__ = [
    "blake2b_personal",
    "blake2s_personal",
    "ripemd160",
    "sha256",
    "sha256d",
]

_PERSONAL_LEN = 16


def sha256(data: bytes) -> bytes:
    """SHA-256."""
    return hashlib.sha256(data).digest()


def sha256d(data: bytes) -> bytes:
    """SHA-256 applied twice, the checksum and legacy txid construction."""
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def ripemd160(data: bytes) -> bytes:
    """RIPEMD-160, used with SHA-256 to derive transparent address hashes.

    Raises:
        ZcashError: if the OpenSSL build backing hashlib omits RIPEMD-160,
            which some distributions do once it leaves the default provider.
    """
    try:
        h = hashlib.new("ripemd160")
    except ValueError as e:  # pragma: no cover - depends on the OpenSSL build
        from pyzcash.errors import ZcashError

        raise ZcashError(
            "this Python's hashlib has no RIPEMD-160, so transparent address "
            "hashes cannot be computed; it usually means OpenSSL was built "
            "without the legacy provider"
        ) from e
    h.update(data)
    return h.digest()


def blake2b_personal(
    personal: bytes, data: bytes, digest_size: int = 32
) -> bytes:
    """BLAKE2b with a personalization string.

    Zcash keys nearly every digest by personalization, so the personalization
    is the first argument: it names which digest this is.

    Args:
        personal: the personalization, at most 16 bytes. Shorter values are
            zero-padded, exactly as BLAKE2b specifies.
        data: the message.
        digest_size: the output length in bytes, 1 to 64.
    """
    if len(personal) > _PERSONAL_LEN:
        raise ValueError(
            f"BLAKE2b personalization is at most {_PERSONAL_LEN} bytes, "
            f"got {len(personal)}"
        )
    return hashlib.blake2b(
        data, digest_size=digest_size, person=personal
    ).digest()


def blake2s_personal(
    personal: bytes, data: bytes, digest_size: int = 32
) -> bytes:
    """BLAKE2s with a personalization string (at most 8 bytes)."""
    return hashlib.blake2s(
        data, digest_size=digest_size, person=personal
    ).digest()
