"""What a field holds after a tool wrote to it, judged one way for every field-entry path; each tool reads the
field back itself and hands the reading here, so no page objects."""

from __future__ import annotations

import decimal
import re
import unicodedata
from enum import Enum
from typing import Any

from skyvern.forge.taskv3.loop import ToolResult

# The observable-state vocabulary a readback compares — the same fields observe reports per element.
# None means "not read"; the classifier treats absence as no-committable-state, never as a value.
_COMMIT_STATE_KEYS = ("value", "checked", "selected", "pressed")


class CommitStatus(str, Enum):
    OK = "ok"  # state moved in the committing direction, read off exactly one element
    DID_NOT_COMMIT = "did_not_commit"  # target readable, and it did NOT commit
    UNVERIFIED = "unverified"  # no readable committable state, or committed but re-resolved to n != 1


def _has_committable_state(state: dict[str, Any] | None) -> bool:
    return isinstance(state, dict) and any(state.get(k) is not None for k in _COMMIT_STATE_KEYS)


def _classify_commit(
    pre: dict[str, Any] | None, post_matches: int, post: dict[str, Any] | None, *, committed_value: bool | None = None
) -> CommitStatus:
    """Classify a value-must-change action from a before/after observable-state readback.

    Ranked fail-closed: a readable did-not-commit is reported whatever the target re-resolved to, because
    an error halts the rest of a batched turn only when it moved the page -- otherwise the field is
    reported unfilled and only its same-selector dependents and any later click or Enter are skipped
    (INV-1 guards the confident ok, not the refusal). A commit read off
    a target that re-resolved to n != 1 is `unverified` (INV-1); no readable committable state is
    `unverified` (INV-2). `committed_value` hands in a caller's own value-dimension truth in place of the
    generic any-field-changed rule.
    """
    if post is None or not _has_committable_state(post):
        return CommitStatus.UNVERIFIED
    if committed_value is None:
        if pre is None or not _has_committable_state(pre):
            return CommitStatus.UNVERIFIED
        committed_value = any(pre.get(k) != post.get(k) for k in _COMMIT_STATE_KEYS)
    if not committed_value:
        return CommitStatus.DID_NOT_COMMIT
    return CommitStatus.OK if post_matches == 1 else CommitStatus.UNVERIFIED


_ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad]")


def _canon_label(text: str) -> str:
    """One canonical form for option text: two labels that render alike compare equal (NFKC, zero-width
    characters dropped, NBSP and runs of whitespace collapsed, casefolded)."""
    folded = unicodedata.normalize("NFKC", _ZERO_WIDTH_RE.sub("", str(text or "")))
    return " ".join(folded.replace("\u00a0", " ").split()).casefold()


def _typed_text_landed(read: str | None, typed: str) -> bool:
    # Exact, whitespace included: a field that reformatted or trimmed the text ("3" -> "03", "abc " ->
    # "abc") is refused rather than judged equivalent; a refusal costs one retry, a wrong equivalence is
    # a false success.
    return read is not None and read == typed


_PLAIN_NUMBER_RE = re.compile(r"[+-]?\d+(?:\.\d+)?", re.ASCII)
# The only renderings a cell may show for a typed number, around whitespace: an optional leading minus, at most one
# currency sign (Unicode Sc) before or after, comma grouping and decimal places. An allow-list: any other affix differs.
_RENDERED_NUMBER_RE = re.compile(r"(-?)(\D?)\s?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s?(\D?)", re.ASCII)
# A leading plus sign or zero belongs to an identifier (a phone number, a postal code), so only the exact text matches.
_IDENTIFIER_NUMBER_RE = re.compile(r"\+|-?0\d")


def _same_cell_value(shown: str, typed: str) -> bool:
    typed = typed.strip()
    if shown == typed:
        return True
    rendered = _RENDERED_NUMBER_RE.fullmatch(shown.strip())
    if rendered is None or not _PLAIN_NUMBER_RE.fullmatch(typed) or _IDENTIFIER_NUMBER_RE.match(typed):
        return False
    sign, before, number, after = rendered.groups()
    if (before and after) or any(c and unicodedata.category(c) != "Sc" for c in (before, after)):
        return False
    # Decimal, not float: long identifiers would round to the same binary value.
    return decimal.Decimal(sign + number.replace(",", "")) == decimal.Decimal(typed)


class EntryMatch(str, Enum):
    SAME = "same"
    # The field holds the typed value in its own format: mask separators added or removed, case changed, or a number
    # rendered with grouping or a currency sign.
    REFORMATTED = "reformatted"
    # Nothing landed: the field is empty, or holds exactly what it held before.
    NOT_HELD = "not_held"
    DIFFERENT = "different"


# What an input mask adds or strips. A dot, sign, "@" or ":" carries meaning ("1.5" is not "15"), so it never does.
_MASK_SEPARATOR_RE = re.compile(r"[\s()\-/]")
# A signed or decimal number is judged as a number only: dropping its sign or point changes the value.
_NUMBER_RE = re.compile(r"\s*[+-]?\d[\d,]*(?:\.\d+)?\s*")


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", _ZERO_WIDTH_RE.sub("", text)).casefold()


def _lead_sign(text: str) -> str:
    lead = text.strip()[:1]
    return lead if lead in ("+", "-") else ""


def _number_changed(typed: str, held: str) -> bool:
    # A sign or decimal point the page added or dropped changes a number ("1500" vs "-1500", "75000.00" vs "7500000");
    # the same sign on both sides is notation, as in "+15551234567" shown as "+1 (555) 123-4567".
    if not (_NUMBER_RE.fullmatch(typed) or _NUMBER_RE.fullmatch(held)):
        return False
    return _lead_sign(typed) != _lead_sign(held) or ("." in typed) != ("." in held)


# Only a date's parts are padded or unpadded by a page; an identifier's leading zero is part of its value.
_DATE_TEXT_RE = re.compile(r"[0-9]{1,4}[/\-.][0-9]{1,2}[/\-.][0-9]{1,4}")


def _groups(text: str) -> list[str]:
    groups = re.findall(r"[^\W_]+", _fold(text))
    if not _DATE_TEXT_RE.fullmatch(text.strip()):
        return groups
    return [str(int(g)) if len(g) <= 2 else g for g in groups]


def _masked_same(typed: str, held: str) -> bool:
    if _number_changed(typed, held):
        return False
    if _MASK_SEPARATOR_RE.search(typed.strip()) and _MASK_SEPARATOR_RE.search(held.strip()):
        # Both separated: the same groups, so "12/1/2024" regrouped as "12/12/024" is not the same date.
        groups = _groups(typed)
        return bool(groups) and groups == _groups(held)
    stripped = _MASK_SEPARATOR_RE.sub("", _fold(typed))
    return bool(stripped) and stripped == _MASK_SEPARATOR_RE.sub("", _fold(held))


def compare_text(typed: str, held: str | None, before: str | None = None, *, exact: bool = False) -> EntryMatch | None:
    """None when the field could not be read. Never a subsequence rule: "12" read back as "123" (residue a clear
    left) or a prior value with the text appended is DIFFERENT, not a match. `exact` is for opaque values such as a
    secret, where a changed case or a dropped space is a different value."""
    if held is None:
        return None
    if held == typed or (not exact and held.strip() and " ".join(held.split()) == " ".join(typed.split())):
        return EntryMatch.SAME
    if not held.strip():
        return EntryMatch.NOT_HELD
    if exact:
        return EntryMatch.NOT_HELD if before is not None and held == before else EntryMatch.DIFFERENT
    if _masked_same(typed, held) or _same_cell_value(held, typed) or ("," in typed and _same_cell_value(typed, held)):
        return EntryMatch.REFORMATTED
    if before is not None and held == before:
        return EntryMatch.NOT_HELD
    return EntryMatch.DIFFERENT


def unconfirmed_ok(content: str, data: dict[str, Any] | None = None) -> ToolResult:
    """An ok whose value the field could not confirm: the loop holds back a later submit in the same batch."""
    return ToolResult("ok", content, data, ok_class="unverified", entry_unconfirmed=True)


def entry_outcome(selector: str, match: EntryMatch | None, *, shown: str | None, did: str = "typed into") -> ToolResult:
    """The one result for a field-entry read-back. `shown` is the held value as the model may see it (masked and
    cut), or None when it must not be echoed (a secret)."""
    if match is None:
        return unconfirmed_ok(
            f"{did} {selector}, but the field could not be read back afterwards, so the value is NOT confirmed. "
            "Re-observe the field before relying on it."
        )
    if match is EntryMatch.SAME:
        return ToolResult.ok(f"{did} {selector}")
    if match is EntryMatch.REFORMATTED:
        return ToolResult.ok(
            f"{did} {selector}; the field shows it as '{shown}'" if shown is not None else f"{did} {selector}",
            ok_class="held_as",
        )
    if match is EntryMatch.NOT_HELD:
        return ToolResult.error(
            f"{did} {selector}, but the field is empty or unchanged afterwards, so the value is NOT entered. "
            "Re-observe before trying again: an editor that draws its text elsewhere may already hold it, and typing "
            "again would add it twice.",
            error_class="text_not_held",
        )
    holds = f"'{shown}'" if shown is not None else "a different value"
    return ToolResult(
        "ok",
        f"{did} {selector}, but the field now holds {holds}, not what was entered: the page may have reformatted, "
        "capped or refused part of it. If that is the value the task needs, the field is filled; if not, fix it "
        "before submitting.",
        ok_class="held_differs",
        entry_unconfirmed=True,
    )
