"""Tests for the _UNSET sentinel value."""

from skyvern.forge.sdk.db._sentinels import _UNSET


def test_unset_is_unique_sentinel() -> None:
    """_UNSET should be distinguishable from None and other values."""
    assert _UNSET is not None
    assert _UNSET is not False
    assert _UNSET != 0
    assert _UNSET != ""


def test_unset_identity() -> None:
    """_UNSET should be the same object across imports."""
    from skyvern.forge.sdk.db._sentinels import _UNSET as second_import

    assert _UNSET is second_import
