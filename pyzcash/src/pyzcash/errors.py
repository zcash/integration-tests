"""Exception hierarchy for pyzcash.

Every failure raised by this library derives from :class:`ZcashError`, so a
caller can guard an entire parse with a single ``except``. External input is
never trusted: decoding functions raise, they do not assert, and they never
return a partially populated object.
"""

from __future__ import annotations

__all__ = [
    "ChecksumError",
    "EncodingError",
    "ParseError",
    "RangeError",
    "TrailingDataError",
    "TruncatedDataError",
    "ZcashError",
]


class ZcashError(Exception):
    """Base class for every error raised by pyzcash."""


class ParseError(ZcashError):
    """A byte string could not be parsed into the requested structure."""


class TruncatedDataError(ParseError):
    """The input ended before a complete structure could be read."""

    def __init__(self, wanted: int, available: int) -> None:
        super().__init__(
            f"truncated input: wanted {wanted} byte(s), {available} available"
        )
        self.wanted = wanted
        self.available = available


class TrailingDataError(ParseError):
    """A structure parsed correctly but bytes remained after it.

    Consensus encodings are exact. Trailing bytes mean the caller was handed
    something other than what they thought, so this is an error rather than
    something to ignore.
    """

    def __init__(self, remaining: int) -> None:
        super().__init__(f"{remaining} byte(s) of trailing data after parsing")
        self.remaining = remaining


class EncodingError(ZcashError):
    """A string encoding (Base58Check, Bech32, Bech32m) is malformed."""


class ChecksumError(EncodingError):
    """A checksummed encoding failed its checksum verification."""


class RangeError(ZcashError, ValueError):
    """A value fell outside the range the consensus rules permit.

    Derives from :class:`ValueError` as well, because callers reasonably expect
    an out-of-range amount to be a ``ValueError``.
    """
