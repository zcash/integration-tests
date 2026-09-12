"""Test helpers."""

from __future__ import annotations

__all__ = ["mutate"]


def mutate(obj: object, name: str, value: object) -> None:
    """Attempt to set an attribute, as untyped code would.

    Taking ``object`` means the call type-checks, so a test can make a genuine
    runtime attempt to mutate a frozen value without a ``type: ignore``. The
    point is to observe what actually happens, not to tell the type checker to
    look away.
    """
    setattr(obj, name, value)
