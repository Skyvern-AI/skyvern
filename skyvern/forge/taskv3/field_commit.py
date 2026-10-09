"""What a field holds after a tool wrote to it, judged one way for every field-entry path; each tool reads the
field back itself and hands the reading here, so no page objects."""

from __future__ import annotations

import dataclasses
import decimal
import difflib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any, Literal, TypeVar

from skyvern.core.script_generations.fuzzy_matcher import match_option_exact_or_stem_with_tier, normalize_option_label
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


_Row = TypeVar("_Row", bound=Mapping[str, Any])


def _exact_tier_key(text: str) -> str:
    """The exact tier's own equality key — `_canon_label` plus the shared matcher's case/apostrophe fold —
    for pre-filters asking "which rows did that tier see as this value". A plain `_canon_label` filter
    folds less than the tier does and would disagree with it, so pre-filters must use this key instead.
    """
    return normalize_option_label(_canon_label(text))


def _prefix_tokens(s: str) -> list[str]:
    # Fold commas and apostrophes so a short value token-prefix-matches a punctuated label ("Yes" → "Yes, I
    # consent"). A slash is left intact so a combined "Yes/No" option is not prefix-matched by "Yes".
    return re.sub(r"[,'’]", " ", s).lower().split()


def _is_forward_prefix(want: list[str], label: list[str]) -> bool:
    return bool(want) and len(want) < len(label) and label[: len(want)] == want


def _row_identity(o: Mapping[str, Any]) -> str:
    """The row's identity as `_MENU_OPTION_TEXTS_JS`'s `rowIdentity` derived it. A row with no `identity` key
    is a note entry, whose `text` is already that identity; an empty one (a row nothing could read) is absent."""
    identity = o.get("identity")
    return identity if isinstance(identity, str) and identity else str(o.get("text") or "")


def _lone_duplicate_candidate(rows: Sequence[Mapping[str, Any]]) -> int | None:
    """Whether ≥2 matched rows are the SAME candidate rendered more than once: canonical row IDENTITY
    agreement across every row is the load-bearing check, and a present aria-label, `val`, or other declared
    value is a VETO on top of it — one disagreeing across otherwise-identical rows marks them distinct, an
    absent one never does. Returns the FIRST row's `n` when the whole set collapses to one candidate, else
    None so the caller keeps refusing.
    """
    if len(rows) < 2:
        return None
    if len({_canon_label(_row_identity(o)) for o in rows}) != 1:
        return None
    # The tagged leaf and its option ancestor are independent name surfaces: a shared leaf label
    # ("Choose") must not mask ancestors that disagree, so each position vetoes on its own.
    for surface in range(2):
        names = {
            _canon_label(str((o.get("labels") or [None, None])[surface]))
            for o in rows
            if (o.get("labels") or [None, None])[surface]
        }
        if len(names) >= 2:
            return None
    present_labels = {_canon_label(str(o.get("label"))) for o in rows if o.get("label")}
    if len(present_labels) >= 2:
        return None
    # Vals are machine identifiers, not display text: compared byte-exact, never case/Unicode-folded,
    # so "ID-A" and "id-a" stay two candidates. `vals` is the collapse's OWN read of the other value
    # surfaces (data-code, data-key, name, title, ...) — unfiltered by the commit verifier's numeric/
    # length drops, so "101" vs "202" is a real disagreement here. Non-empty sets must agree byte-exact;
    # an empty set says nothing and cannot contradict.
    present_vals = {str(o.get("val")) for o in rows if o.get("val") is not None}
    if len(present_vals) >= 2:
        return None
    # Two-rule agreement over the surface+attribute-keyed entries. (1) A key present on BOTH rows
    # must carry one value — crossed values across surfaces refuse, while a value carried on one
    # row's surface only says nothing against the other row. (2) The depth-blind floor: one row's
    # flattened attr=value pairs must nest inside the other's. Rows declaring identity on disjoint
    # attributes cannot be confirmed the same, and an agreeing generic attribute (a shared title)
    # beside disjoint identifiers is ordinary markup, not identity evidence — the pairs don't nest,
    # so it refuses. A row declaring NOTHING still constrains nothing (its empty set nests, so an
    # attribute-less a11y copy collapses).
    val_maps: list[dict[str, str]] = []
    for o in rows:
        entries: dict[str, str] = {}
        for raw in o.get("vals") or []:
            key, _, val_part = str(raw).partition("=")
            entries[key] = val_part
        val_maps.append(entries)
    for i, first in enumerate(val_maps):
        for second in val_maps[i + 1 :]:
            for shared in first.keys() & second.keys():
                if first[shared] != second[shared]:
                    return None
            flat_first = {f"{k.partition(':')[2]}={v}" for k, v in first.items()}
            flat_second = {f"{k.partition(':')[2]}={v}" for k, v in second.items()}
            if not (flat_first <= flat_second or flat_second <= flat_first):
                return None
    n = rows[0].get("n")
    return n if isinstance(n, int) else None


def _without_nested_copies(value: str, rows: Sequence[_Row]) -> list[_Row]:
    """Drop an exact-match row that holds another exact-match row: a wrapper and the element carrying its
    label are one candidate, and the innermost stands for it. `inside` is a row's nearest tagged ancestor;
    siblings never share a chain, so identical siblings stay separate and are refused."""
    want = _exact_tier_key(value)
    exact = {o.get("n") for o in rows if _exact_tier_key(str(o.get("text") or "")) == want}
    by_n: dict[Any, Mapping[str, Any]] = {o.get("n"): o for o in rows}
    wrappers: set[Any] = set()
    for n in exact:
        seen: set[Any] = set()
        up = (by_n.get(n) or {}).get("inside")
        while up is not None and up not in seen:
            if up in exact:
                wrappers.add(up)
            seen.add(up)
            up = (by_n.get(up) or {}).get("inside")
    return [o for o in rows if o.get("n") not in wrappers]


class Tier(str, Enum):
    EXACT = "exact"
    PRIMARY = "primary"
    STEM = "stem"
    PREFIX = "prefix"


# PRIMARY and STEM are one rank: each is the label up to a decoration or an inflection, so hits across them compete.
_TIER_RANK = {Tier.EXACT: 0, Tier.PRIMARY: 1, Tier.STEM: 1, Tier.PREFIX: 2}
ALL_TIERS = frozenset(Tier)
OFFER_WHY = {
    Tier.EXACT: "is it",
    Tier.PRIMARY: "its main label is it",
    Tier.STEM: "differs from it only by singular/plural",
    Tier.PREFIX: "starts with it",
}
_TIER_NOTES = {
    Tier.PRIMARY: "its main label is {value}",
    Tier.STEM: "it is the only option matching {value} by singular/plural",
    Tier.PREFIX: "it is the only option starting with {value}",
}

RefusalReason = Literal["none", "identical", "ambiguous", "incomplete", "reduced_query", "unsettled", "held"]


@dataclasses.dataclass(frozen=True)
class Pick:
    n: int
    text: str
    tier: Tier


@dataclasses.dataclass(frozen=True)
class Refusal:
    reason: RefusalReason
    contenders: tuple[Mapping[str, Any], ...] = ()


@dataclasses.dataclass(frozen=True)
class Offer:
    row: Mapping[str, Any]
    tier: Tier | None


_TRAILING_GROUP_RE = re.compile(r"^(.*?\S)\s*\([^()]*\)$")
_SPACED_DASH_RE = re.compile(r"\s+[-\u2013\u2014]\s+")
_DIAL_CODE_RE = re.compile(r"\s+\+\d[\d\s-]{0,6}$")


def _without_leading_pictographs(text: str) -> tuple[str, bool]:
    # Only pictographs and flags (Unicode So, with their joiners and modifiers) are decoration. A sign such as "<", ">"
    # or "~" changes what the row means ("< 1 year" is not "1 year").
    i, seen = 0, False
    while i < len(text) and (unicodedata.category(text[i]) in ("So", "Sk") or text[i] in "\ufe0f\u200d"):
        seen = seen or unicodedata.category(text[i]) == "So"
        i += 1
    if not (seen and text[i : i + 1].isspace()):
        return text, False
    # Only a flag (a regional-indicator pair) marks the row as a country, which is what makes a trailing "+N" a code.
    return text[i:].lstrip(), any(0x1F1E6 <= ord(c) <= 0x1F1FF for c in text[:i])


def primary_parts(text: str, dom_primary: str | None = None) -> tuple[str, ...]:
    # Each part is ONE typographic cut: the one trailing group `_label_names_value` allows, the spaced dash `_lead_clause`
    # splits on, or a dialing code. Unlike `_lead_clause`, never a comma: "City, Region" heads a hierarchy. A trailing
    # "+N" is a dialing code only beside a leading flag; elsewhere it is part of the label ("👥 Employee +1").
    body, flagged = _without_leading_pictographs(" ".join(text.split()))
    group = _TRAILING_GROUP_RE.match(body)
    dialed = _DIAL_CODE_RE.sub("", body) if flagged else ""
    parts = [body, group.group(1) if group else "", _SPACED_DASH_RE.split(body, 1)[0], dialed]
    if dom_primary and " ".join(dom_primary.split()) != " ".join(text.split()):
        parts.append(dom_primary)
    return tuple(dict.fromkeys(p for p in parts if p.strip()))


def _dom_primary(row: Mapping[str, Any]) -> str | None:
    return row["label"] if isinstance(row.get("label"), str) else None


def tier_of(value: str, row: Mapping[str, Any], allow: frozenset[Tier] = ALL_TIERS) -> Tier | None:
    """How `row` names `value`, read on the row side only: a shorter row never names a longer value. A main label
    `allow` leaves out is read at the tiers below it."""
    text, want = str(row.get("text") or ""), _exact_tier_key(value)
    if not want:
        return None
    if _exact_tier_key(text) == want:
        return Tier.EXACT
    if Tier.PRIMARY in allow and any(_exact_tier_key(p) == want for p in primary_parts(text, _dom_primary(row))):
        return Tier.PRIMARY
    if match_option_exact_or_stem_with_tier(_canon_label(value), [_canon_label(text)])[1] == "stem":
        return Tier.STEM
    return Tier.PREFIX if _is_forward_prefix(_prefix_tokens(value), _prefix_tokens(text)) else None


def _collapse_exact(value: str, rows: Sequence[Mapping[str, Any]], bare: frozenset[int]) -> int | None:
    # A declared row outranks a bare node wearing its label. Bare rows carry no identity to veto on, so among them only
    # nesting tells one candidate's copies from siblings.
    declared = [o for o in rows if o.get("n") not in bare]
    if len(declared) == 1:
        return declared[0]["n"]
    if declared:
        return _lone_duplicate_candidate(declared)
    survivors = _without_nested_copies(value, rows)
    return survivors[0]["n"] if len(survivors) == 1 else None


def match_choice(
    value: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    complete: bool,
    query_full: bool = True,
    allow: frozenset[Tier] = ALL_TIERS,
    collapse: bool = False,
    bare: frozenset[int] = frozenset(),
) -> Pick | Refusal:
    """Which listed row is `value`. Exact rows that are not one candidate refuse before any lower tier. A non-exact hit
    commits only when it is unique at the best rank (leaves sharing a `unit` are one row), its tier is allowed, the
    query was the value itself and the list was read whole."""
    graded = [(o, t) for o in rows if isinstance(o.get("n"), int) and (t := tier_of(value, o, allow)) is not None]
    exact = [o for o, t in graded if t is Tier.EXACT]
    if exact:
        n = exact[0]["n"] if len(exact) == 1 else _collapse_exact(value, exact, bare) if collapse else None
        if n is None:
            return Refusal("identical", tuple(exact))
        return Pick(n, next(str(o.get("text") or "") for o in exact if o["n"] == n), Tier.EXACT)
    if not graded:
        return Refusal("none")
    rank = min(_TIER_RANK[t] for _, t in graded)
    units: dict[object, tuple[Mapping[str, Any], Tier]] = {}
    for o, t in graded:
        if _TIER_RANK[t] == rank:
            # Leaves of one declared row are one option only while they name the same thing: two controls inside one row,
            # each with its own label, stay two contenders.
            unit = (
                (o["unit"], _dom_primary(o) or str(o.get("text") or "")) if isinstance(o.get("unit"), int) else o["n"]
            )
            units.setdefault(unit, (o, t))
    contenders = tuple(o for o, _ in units.values())
    hit: tuple[Mapping[str, Any], Tier] | None = next(iter(units.values())) if len(units) == 1 else None
    if hit is None and collapse and (n := _lone_duplicate_candidate(contenders)) is not None:
        hit = next(u for u in units.values() if u[0]["n"] == n)
    if hit is None:
        return Refusal("ambiguous", contenders)
    if hit[1] not in allow:
        return Refusal("none", contenders)
    if not query_full:
        return Refusal("reduced_query", contenders)
    if not complete:
        return Refusal("incomplete", contenders)
    return Pick(hit[0]["n"], str(hit[0].get("text") or ""), hit[1])


def contender_texts(value: str, texts: Sequence[str]) -> list[str]:
    """The texts any tier takes for `value`, one entry per row."""
    return [t for t in texts if tier_of(value, {"text": t}) is not None]


def nearest(value: str, rows: Sequence[Mapping[str, Any]], k: int = 3) -> tuple[Offer, ...]:
    """The rows most like `value`: tier hits by rank, then shared words, then string similarity, then list order. A row
    sharing no word and under half similar is left out."""
    want_words, want, seen = set(_prefix_tokens(value)), _canon_label(value), set()
    ranked: list[tuple[tuple[float, ...], Offer]] = []
    for i, o in enumerate(rows):
        text = str(o.get("text") or "")
        key = _exact_tier_key(text)
        if not key or key in seen or o.get("nav"):
            continue
        seen.add(key)
        parts = primary_parts(text, _dom_primary(o))
        shared = len(want_words & set(_prefix_tokens(" ".join(parts))))
        matchers = [difflib.SequenceMatcher(None, want, _canon_label(p)) for p in parts]
        tier = tier_of(value, o)
        # quick_ratio() bounds ratio() from above, so a row it already rules out skips the full comparison on long lists.
        if tier is None and not shared and max(m.quick_ratio() for m in matchers) < 0.5:
            continue
        ratio = max(m.ratio() for m in matchers)
        if tier is None and not shared and ratio < 0.5:
            continue
        ranked.append(((_TIER_RANK[tier] if tier else 3, -shared, -ratio, i), Offer(o, tier)))
    ranked.sort(key=lambda r: r[0])
    return tuple(offer for _, offer in ranked[:k])


def with_tier_note(result: ToolResult, tier: Tier | None, shown_value: str) -> ToolResult:
    """Names the inference behind an ok whose row is not the value exactly; `shown_value` is as the model may see it."""
    if result.status != "ok" or tier is None or tier is Tier.EXACT:
        return result
    note = _TIER_NOTES[tier].format(value=repr(shown_value))
    return dataclasses.replace(
        result,
        content=f"{result.content}; not an exact match: {note}",
        data={**(result.data or {}), "choice": {"tier": tier.value}},
    )
