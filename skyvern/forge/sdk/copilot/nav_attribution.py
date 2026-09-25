"""Who owns a navigation failure: Skyvern's egress, or the target.

Decided from the codes the driver itself reported — carried on the block result's ``error_codes``
— never from the failure sentence. That sentence is model-authored on some paths
(``skyvern/forge/agent.py`` copies a task's reasoning into ``failure_reason`` verbatim), so a page
or a model can reproduce any wording; text can never say who wrote it.

``error_codes`` also carries a block's declared error code, which the workflow author writes. The
codes below are ``net::`` tokens a declaration would have to impersonate deliberately; that is a
narrower surface than free prose, not an absent one.

A leaf because enforcement imports runtime authoring repair, so the predicate cannot live in
enforcement and still be read by both.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator

from skyvern.constants import PROXY_TRANSPORT_NAV_ERRORS, SKIP_INNER_NAV_RETRY_ERRORS
from skyvern.exceptions import NO_ADDRESS_RECORD_NAV_ERROR_CODE

_DRIVER_NAV_ERROR_CODE = re.compile(r"net::ERR_[A-Z0-9_]+")

TERMINAL_NAV_ERROR_CODES = tuple(code for code in SKIP_INNER_NAV_RETRY_ERRORS if code not in PROXY_TRANSPORT_NAV_ERRORS)

# NO_ADDRESS_RECORD_NAV_ERROR_CODE is a synthetic Skyvern sentinel, not one of the codes Chromium
# itself reports (contrast PROXY_TRANSPORT_NAV_ERRORS, which are). It stands in for a
# resolver-corroborated dead host -- the driver's own code for that case is a borrowed proxy
# transport code -- so it belongs on the target side of the split even though it is not one.
_TARGET_OWNED_NAV_ERROR_CODES = (*TERMINAL_NAV_ERROR_CODES, NO_ADDRESS_RECORD_NAV_ERROR_CODE)


def proxy_owns_nav_codes(codes: Iterable[str | None]) -> bool:
    """True when the driver's own codes name Skyvern's proxy hop and none of them names the target.

    Callers must pass codes the driver produced — a block's ``error_codes`` or the browser state's
    recorded code — never tokens recovered from a failure sentence.
    """
    reported = [code for code in codes if isinstance(code, str) and code]
    if not reported:
        return False
    # constants.py lists ERR_CERT_ and ERR_SSL_ as prefixes, not whole codes, so a target-owned
    # code has to be matched by prefix or every certificate failure reads as unclaimed.
    if any(code.startswith(terminal) for code in reported for terminal in _TARGET_OWNED_NAV_ERROR_CODES):
        return False
    return any(code in PROXY_TRANSPORT_NAV_ERRORS for code in reported)


def target_owns_nav_codes(codes: Iterable[str | None]) -> bool:
    """True when the driver's own codes name the target: DNS, certificate, SSL, a refused URL, or a
    resolver-corroborated dead host.

    Same contract as :func:`proxy_owns_nav_codes` -- the codes must be ones the driver produced,
    never tokens recovered from a failure sentence.
    """
    reported = [code for code in codes if isinstance(code, str) and code]
    return any(code.startswith(terminal) for code in reported for terminal in _TARGET_OWNED_NAV_ERROR_CODES)


def _declared_error_code(source: dict[str, object]) -> str | None:
    """The error code this block's author declared and raised, if that is how it failed.

    Such a code lands in ``error_codes`` beside driver-reported ones, and the author picks its
    spelling -- in a copilot session that author is the model. Only the declared path names it in
    the output, so it can be dropped without discarding what a driver reported alongside it.
    """
    output = source.get("output")
    declared = output.get("declared_error_code") if isinstance(output, dict) else None
    return declared if isinstance(declared, str) and declared else None


def driver_nav_code_positions(result: object) -> list[tuple[int, int, str]]:
    """``(holder, index, code)`` for each driver navigation code in a run result's ``error_codes``.

    Holder 0 is the run, then each block in order. A caller that has to scrub the result snapshots
    these first: a registered value occurring inside a code ("net") would otherwise corrupt the verdict.
    Declared codes are left out, since their author chose the spelling.
    """
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        return []
    blocks = data.get("blocks")
    holders = [data, *(blocks if isinstance(blocks, list) else [])]
    return [
        (holder_index, code_index, code)
        for holder_index, holder in enumerate(holders)
        if isinstance(holder, dict) and isinstance(holder.get("error_codes"), list)
        for code_index, code in enumerate(holder["error_codes"])
        if isinstance(code, str) and _DRIVER_NAV_ERROR_CODE.fullmatch(code) and code != _declared_error_code(holder)
    ]


def restore_driver_nav_codes(result: object, positions: list[tuple[int, int, str]]) -> None:
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        return
    blocks = data.get("blocks")
    holders = [data, *(blocks if isinstance(blocks, list) else [])]
    for holder_index, code_index, code in positions:
        holder = holders[holder_index] if holder_index < len(holders) else None
        codes = holder.get("error_codes") if isinstance(holder, dict) else None
        if isinstance(codes, list) and code_index < len(codes):
            codes[code_index] = code


def _codes_of(source: object) -> list[str]:
    if not isinstance(source, dict):
        return []
    declared = _declared_error_code(source)
    return [code for code in source.get("error_codes") or [] if isinstance(code, str) and code and code != declared]


def _reason_of(source: dict[str, object]) -> str:
    reason = source.get("failure_reason")
    return reason if isinstance(reason, str) and reason else ""


def iter_failure_reason_codes(result: object) -> Iterator[tuple[str, list[str]]]:
    """Each failure a reader may see, paired with the codes reported for that same failure.

    Paired by position rather than by matching reason text back to a block: two blocks can report
    the same sentence, and resolving both occurrences to one of them hides the other's verdict.
    Newest block first, matching the order a displayed reason is chosen in.
    """
    if not isinstance(result, dict):
        return
    data = result.get("data")
    data = data if isinstance(data, dict) else {}
    blocks = [block for block in data.get("blocks") or [] if isinstance(block, dict)]
    run_level = data.get("failure_reason")
    if isinstance(run_level, str) and run_level:
        # The run reported the failure it ended on, and the block that reported that failure answers
        # for it: the newest one that failed, which is also the sentence a reader is shown. A block
        # the run was told to continue past is a failure the run carried on from, not this one.
        ended_on = next((block for block in reversed(blocks) if _reason_of(block) or _codes_of(block)), None)
        run_codes = _codes_of(data)
        if ended_on is None or not _reason_of(ended_on):
            # Nothing reported a sentence of its own, so the run's answers -- with the codes of the
            # block it ended on, which would otherwise carry a verdict nothing reports.
            yield run_level, run_codes or _codes_of(ended_on)
            return
        if run_codes:
            yield run_level, run_codes
        blocks = [ended_on]
    for block in reversed(blocks):
        reason = _reason_of(block)
        if reason:
            yield reason, _codes_of(block)


def block_nav_error_codes(result: object, reason: str | None = None) -> list[str]:
    """The driver-reported codes behind the reason a reader will see.

    With a ``reason``, only the block that reported it answers: a run whose first block timed out
    and whose second hit the proxy must not have the timeout attributed to Skyvern's egress. Without
    one, every code on the result answers.
    """
    if not isinstance(result, dict):
        return []
    data = result.get("data")
    data = data if isinstance(data, dict) else {}
    blocks = [block for block in data.get("blocks") or [] if isinstance(block, dict)]
    if reason is not None:
        # Newest first: the displayed reason is selected from the newest block, so two blocks
        # sharing reason text would otherwise attribute the older one's codes.
        owner = next((block for block in reversed(blocks) if block.get("failure_reason") == reason), None)
        if owner is not None:
            return _codes_of(owner)
        # A reason no block reported -- a connect failure, or text the recorder composed -- is not
        # any one block's, so the run answers for it.
    codes: list[str] = []
    for source in (data, *blocks):
        for code in _codes_of(source):
            if code not in codes:
                codes.append(code)
    return codes
