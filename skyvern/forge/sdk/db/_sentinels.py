"""Sentinel values for the database layer."""

from enum import Enum


class _Unset(Enum):
    TOKEN = "unset"


# Sentinel for distinguishing "not passed" from "passed as None" in update methods; a single-member Enum so
# `x is not _UNSET` narrows a `T | _Unset` parameter to T.
_UNSET = _Unset.TOKEN
