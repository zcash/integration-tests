"""Loader for the canonical Zcash test vectors.

The JSON files under tests/vectors_json/ are vendored verbatim from
zcash/zcash-test-vectors, pinned to the commit recorded in
tests/vectors_json/COMMIT. They are the same vectors librustzcash, the sapling
and orchard crates, and zcashd itself test against, so agreeing with them is the
strongest evidence available that this implementation reads Zcash correctly and
not merely consistently with itself.

Each file is a list whose first element is a provenance note, whose second
element names the columns, and whose remaining elements are rows. This module
turns that into a list of dicts.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Final

__all__ = ["VECTOR_DIR", "load_vectors", "pinned_commit", "provenance"]

VECTOR_DIR: Final = Path(__file__).parent / "vectors_json"


def _read(name: str) -> list[list[object]]:
    raw = json.loads((VECTOR_DIR / f"{name}.json").read_text())
    if not isinstance(raw, list):
        raise TypeError(f"{name}.json is not a list of rows")
    return raw


@cache
def load_vectors(name: str) -> tuple[dict[str, object], ...]:
    """The rows of a vector file, as dicts keyed by column name.

    Cached, because several test modules read the same file and the largest is
    over a hundred kilobytes.
    """
    raw = _read(name)
    header = raw[1][0]
    if not isinstance(header, str):
        raise TypeError(f"{name}.json has no column header")
    columns = [c.strip() for c in header.split(",")]
    return tuple(
        dict(zip(columns, row, strict=True))
        for row in raw[2:]
        if isinstance(row, list)
    )


def provenance(name: str) -> str:
    """The upstream source note the vector file carries."""
    note = _read(name)[0][0]
    return note if isinstance(note, str) else ""


def pinned_commit() -> str:
    """The zcash-test-vectors commit the vendored files came from."""
    return (VECTOR_DIR / "COMMIT").read_text().strip()
