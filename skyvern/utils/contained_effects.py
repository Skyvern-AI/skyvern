"""Containment for recording side effects that must not reach the caller's outcome.

Observation and bookkeeping -- log emits, span attributes, Temporal search-attribute
writes -- routinely sit on a critical path: inside a ``finally``, in a finalizer, or
after the terminal write that already decided the run. A failure there should cost the
record, never the work.

A plain ``try``/``except`` is not containment when the handler re-enters the sink that
just failed, so the report path below is itself suppressed. ``BaseException`` is
deliberately not caught: cancellation and interpreter shutdown must still propagate.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog

LOG = structlog.get_logger()


@contextmanager
def contained_effect(effect: str, /, **context: Any) -> Iterator[None]:
    """Run a recording side effect so that its failure cannot change what the caller does.

    ``effect`` names what was being recorded, and ``context`` adds identifying fields to
    the report. Build the payload inside the block too -- a sink that cannot fail is of
    no help when the arguments to it can.
    """
    try:
        yield
    except Exception as exc:
        _report_contained_failure(effect, exc, context)


def _report_contained_failure(effect: str, exc: Exception, context: dict[str, Any]) -> None:
    fields: dict[str, Any] = dict(context)
    fields["effect"] = effect
    fields["error_type"] = type(exc).__name__
    # The sink being protected may be this same logger, so the report cannot be allowed
    # to re-raise what the guard just swallowed.
    with contextlib.suppress(Exception):
        LOG.warning("Contained side effect failed", **fields)
