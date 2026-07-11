"""The exception hierarchy is part of the public API, so it is tested."""

from __future__ import annotations

import pytest

from pyzcash import (
    ChecksumError,
    EncodingError,
    ParseError,
    RangeError,
    TrailingDataError,
    TruncatedDataError,
    ZcashError,
)


def test_every_error_derives_from_zcash_error() -> None:
    for exc in (
        ChecksumError,
        EncodingError,
        ParseError,
        RangeError,
        TrailingDataError,
        TruncatedDataError,
    ):
        assert issubclass(exc, ZcashError)


def test_checksum_error_is_an_encoding_error() -> None:
    assert issubclass(ChecksumError, EncodingError)


def test_truncation_and_trailing_data_are_parse_errors() -> None:
    assert issubclass(TruncatedDataError, ParseError)
    assert issubclass(TrailingDataError, ParseError)


def test_range_error_is_also_a_value_error() -> None:
    """Callers reasonably expect an out-of-range amount to be a ValueError."""
    assert issubclass(RangeError, ValueError)
    with pytest.raises(ValueError):
        raise RangeError("amount out of range")


def test_truncated_data_error_reports_what_it_wanted() -> None:
    err = TruncatedDataError(wanted=32, available=7)
    assert err.wanted == 32
    assert err.available == 7
    assert "wanted 32" in str(err)


def test_trailing_data_error_reports_the_remainder() -> None:
    err = TrailingDataError(remaining=4)
    assert err.remaining == 4
    assert "4 byte(s) of trailing data" in str(err)
