"""The Task V3 action-row label: a floor phrase from a fixed vocabulary, enriched with the element's
page-visible name only when that name passes both a credential-shape filter and the secret matcher."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection
from typing import Any

from skyvern.forge.sdk.copilot.secret_redaction import raw_secret_spans_for_prompt
from skyvern.utils.secret_redaction import expand_secret_encodings

# Transport bound on a captured element name, applied page-side by the probe and re-applied here.
# A capture that REACHES it is not a name -- it is page text -- so `clean_target_label` drops it
# rather than returning a slice. That is also what keeps the cap from becoming a redaction bypass:
# secret scrubbing matches whole values, so a sliced-up secret would no longer match its registered
# form and would survive into a persisted, displayed row.
TARGET_NAME_CAP = 400

# Control characters plus the format codepoints a page could forge a timeline line with or reverse
# one by. NARROWER than the download-notice scrub on purpose: U+200C/U+200D are ordinary joiners in
# Persian, Hindi and emoji sequences, and stripping them corrupts real element names rather than
# sanitizing them. Line separators are absent because whitespace is collapsed before this runs.
_ELEMENT_NAME_SANITIZE_RE = re.compile("[\\x00-\\x1f\\x7f\\u061c\\u200b\\u200e\\u200f\\u202a-\\u202e\\u2066-\\u2069]")


def clean_target_label(raw: Any) -> str | None:
    """Page-controlled text on its way to a persisted row and the run timeline: strip what could forge
    a line or reverse one, then collapse. None for anything that cleans away to nothing, and for
    anything that arrived at the transport cap (see TARGET_NAME_CAP)."""
    if not isinstance(raw, str):
        return None
    # The cap is judged on the RAW length, before anything is stripped. Cleaning only ever shortens,
    # so judging the cleaned length would let a capture that WAS truncated shrink back under the
    # threshold and be accepted -- 350 zero-width characters ahead of a credential arrive at exactly
    # the cap, clean down to a 50-character fragment, and that fragment no longer matches the
    # registered secret it was cut from.
    if len(raw) >= TARGET_NAME_CAP:
        return None
    # Whitespace collapses FIRST: a newline between two words is a word boundary, and deleting it as
    # a control character would glue them ("Continue\nto pay" -> "Continueto pay"). What the
    # sanitizer then drops -- zero-width, bidi controls -- separates nothing, so a second collapse
    # only closes the gap where one sat between two words.
    return " ".join(_ELEMENT_NAME_SANITIZE_RE.sub("", " ".join(raw.split())).split()) or None


# Unicode CATEGORIES a page can hide inside a credential without a reader seeing anything: Cf
# (format -- zero-width, bidi controls, word joiner, BOM), Mn/Me (combining marks -- U+034F and the
# variation selectors U+FE00-FE0F are Mn, NOT Cf, and render invisibly), Cc (control). Stripped by
# category rather than by an enumerated codepoint list, because enumerating is exactly what kept
# losing on this surface: every list was complete until someone found the character not on it.
_SECRET_MATCH_STRIP_CATEGORIES = frozenset({"Cc", "Cf", "Mn", "Me"})


def _canonical_for_secret_match(value: str) -> str:
    """One shape for both sides of the secret check. NFKD folds compatibility forms and splits every
    accent into a mark the strip then removes, so an invisible character wedged between a letter and
    its accent, or a casefold that emits a combining dot (`'İ'`), cannot split the match."""
    folded = unicodedata.normalize("NFKD", unicodedata.normalize("NFKD", value).casefold())
    return "".join(
        ch for ch in folded if not (ch.isspace() or unicodedata.category(ch) in _SECRET_MATCH_STRIP_CATEGORIES)
    )


def target_label_hides_a_secret(raw: Any, secret_values: Collection[str]) -> bool:
    """Whether a captured element name touches a registered secret, in any shape.

    A name that does is DROPPED, not scrubbed -- and that is the whole point. Scrubbing has to match
    exactly, so it kept losing to whatever normalization the page-side probe had already applied (a
    transport cap, a whitespace collapse); every fix closed one gap and left the next. Dropping does
    not care about the shape, which lets this check be as lenient as it likes: a false positive costs
    one missing timeline label, where a false negative persists a credential to the database and the
    UI. Run on the RAW capture, upstream of every transform this side applies."""
    if not isinstance(raw, str) or not secret_values:
        return False
    haystack = _canonical_for_secret_match(raw)
    if not haystack:
        return False
    # A secret that canonicalizes to nothing would otherwise match every label and silently blank the
    # whole timeline.
    # Every encoded form the shared redaction treats as the secret (URL, JSON, HTML, Base64): a page
    # can render the credential in any of them.
    needles = (_canonical_for_secret_match(e) for v in secret_values for e in expand_secret_encodings(v))
    return any(needle and needle in haystack for needle in needles)


# The shape filter is the primary defense: a default (unmasked) run registers no secrets, so the
# matcher above has nothing to compare against. It is an ALLOWLIST: a name is used only if it
# positively reads like a UI label, and anything unusual falls to the role floor by default, so a new
# credential shape needs no new rule. Strictness is cheap here, since the fallback is "Clicked a button".
# The model cannot do this (cloud_docs/task-v3/CHARTER.md, "Is the harness doing the model's job?"): the
# label must be produced with no LLM call, and the check decides what may be persisted, not what the page
# means for the next action.
TARGET_LABEL_MAX_CHARS = 40

_SHAPE_STRIP_CATEGORIES = frozenset({"Cc", "Cf", "Mn", "Me"})

# Besides letters, digits and spaces, the only characters ordinary labels use.
_LABEL_PUNCTUATION = ".,:?!'\"‘’“”-–—&()…"

_DIGIT_RUN_RE = re.compile(r"\d{4,}")
# A dot inside a word is a domain, an email or a token segment, not prose.
_INNER_DOT_RE = re.compile(r"\w\.\w")
_HEX_BLOB_RE = re.compile(r"[0-9a-f]{12,}", re.IGNORECASE)
# Case-sensitive: each is a vendor prefix issued in a fixed case, and folding would match ordinary words.
_KEY_PREFIX_RE = re.compile(r"(?<![A-Za-z0-9])(?:sk|pk|rk)-|xox[abprs]-|AKIA|eyJ")
_BEARER_RE = re.compile(r"(?i)\bbearer\s")


def _shape_canonical(raw: str) -> str:
    normalized = unicodedata.normalize("NFKC", raw)
    stripped = "".join(ch for ch in normalized if unicodedata.category(ch) not in _SHAPE_STRIP_CATEGORIES)
    return " ".join(stripped.split())


def _reads_like_a_word(word: str) -> bool:
    # > 20, not >=: a real compound word reaches 20 with no separator ("Datenschutzerklärung").
    if len(word) > 20:
        return False
    if len(word) >= 6 and any(ch.isalpha() for ch in word) and any(ch.isdigit() for ch in word):
        return False
    # Random mixed case ("aHVudGVy") flips from lower to upper more than once; a brand ("LinkedIn") once.
    return sum(1 for a, b in zip(word, word[1:]) if a.islower() and b.isupper()) < 2


def name_looks_like_a_label(raw: str) -> bool:
    """Whether a captured name positively reads like a human UI label. Length is judged on the raw
    capture; everything else on the NFKC, invisible-stripped form."""
    if not isinstance(raw, str) or len(raw) > TARGET_LABEL_MAX_CHARS:
        return False
    canonical = _shape_canonical(raw)
    if not canonical or any(not (ch.isalnum() or ch == " " or ch in _LABEL_PUNCTUATION) for ch in canonical):
        return False
    # Spacing is ignorable to a reader: "1 2 3 4" is still a PIN.
    squeezed = canonical.replace(" ", "")
    if 2 * sum(1 for ch in squeezed if ch.isalpha()) < len(squeezed) or _DIGIT_RUN_RE.search(squeezed):
        return False
    if not all(_reads_like_a_word(token.strip(_LABEL_PUNCTUATION)) for token in canonical.split(" ")):
        return False
    if _INNER_DOT_RE.search(canonical) or _HEX_BLOB_RE.search(canonical) or _KEY_PREFIX_RE.search(canonical):
        return False
    if _BEARER_RE.search(canonical):
        return False
    return not raw_secret_spans_for_prompt(canonical)


# How a v3 tool call reads once the target has a name. Only tools that act on ONE named element are
# here: press_key is about the key, navigate about the URL, and neither reads as a target.
TARGET_VERBS = {
    "click": "Clicked",
    "hover": "Hovered over",
    "type": "Typed into",
    "select_option": "Selected an option in",
    "select_combobox": "Selected an option in",
    "file_upload": "Uploaded a file to",
}

# The bounded `kind` vocabulary: a page-supplied or tool-defaulted token to (floor phrase, named noun).
# Only ever used as a DICT KEY -- an unknown token falls through to the tool's own default (or the
# generic floor below), so nothing outside this fixed set can reach the output.
_KIND_NOUNS: dict[str, tuple[str, str]] = {
    "button": ("a button", "button"),
    "link": ("a link", "link"),
    "checkbox": ("a checkbox", "checkbox"),
    "radio": ("a radio button", "radio button"),
    "switch": ("a switch", "switch"),
    "tab": ("a tab", "tab"),
    "menuitem": ("a menu item", "menu item"),
    "option": ("an option", "option"),
    "combobox": ("a dropdown", "dropdown"),
    "select": ("a dropdown", "dropdown"),
    "listbox": ("a list", "list"),
    "textbox": ("a text field", "field"),
    "textarea": ("a text area", "field"),
    "searchbox": ("a search box", "search box"),
    "search": ("a search box", "search box"),
    "email": ("an email field", "field"),
    "password": ("a password field", "field"),
    "tel": ("a phone number field", "field"),
    "url": ("a URL field", "field"),
    "number": ("a number field", "field"),
    "date": ("a date field", "field"),
    "file": ("an upload field", "upload field"),
    "slider": ("a slider", "slider"),
    "image": ("an image", "image"),
    "heading": ("a heading", "heading"),
    "cell": ("a table cell", "cell"),
    "treeitem": ("a tree item", "item"),
}

# The page-side probe's `role` allowlist is built from this, not typed out a second time in JS --
# the only way a JS constant and a Python dict never drift out of sync.
TARGET_KIND_TOKENS = frozenset(_KIND_NOUNS)

# No kind, or a kind this vocabulary does not recognize: the floor cannot name what the element is,
# only that the tool acted on one.
_GENERIC_KIND_NOUNS: tuple[str, str | None] = ("an element", None)

# The kind to assume when the page-supplied `kind` is absent or unrecognized. click/hover are absent
# on purpose: nothing about either verb implies a control shape, so they fall through to the generic
# floor rather than guessing one.
_DEFAULT_KIND_BY_TOOL = {
    "type": "textbox",
    "select_option": "combobox",
    "select_combobox": "combobox",
    "file_upload": "file",
}


_FAILED_TARGET_VERBS = {
    "click": "Tried to click",
    "hover": "Tried to hover over",
    "type": "Tried to type into",
    "select_option": "Tried to select an option in",
    "select_combobox": "Tried to select an option in",
    "file_upload": "Tried to upload a file to",
}


def compose_target_intention(
    tool: str, name: Any, kind: Any, secret_values: Collection[str], *, succeeded: bool = True
) -> str | None:
    """The LLM-free timeline label for one v3 tool call. `kind` is only a lookup key, so the floor never
    contains page text; `name` enriches it only after passing the shape filter and the secret matcher."""
    verb = (TARGET_VERBS if succeeded else _FAILED_TARGET_VERBS).get(tool)
    if verb is None:
        return None
    resolved_kind = kind if isinstance(kind, str) and kind in _KIND_NOUNS else _DEFAULT_KIND_BY_TOOL.get(tool)
    floor_phrase, named_noun = _GENERIC_KIND_NOUNS if resolved_kind is None else _KIND_NOUNS[resolved_kind]
    display = _enrichable_name(name, secret_values)
    if display is None:
        return f"{verb} {floor_phrase}"
    if named_noun is None:
        return f'{verb} "{display}"'
    folded = display.casefold()
    if folded == named_noun or folded.endswith(f" {named_noun}"):
        return f'{verb} the "{display}"'
    return f'{verb} the "{display}" {named_noun}'


def _enrichable_name(name: Any, secret_values: Collection[str]) -> str | None:
    if not isinstance(name, str) or not name_looks_like_a_label(name):
        return None
    if target_label_hides_a_secret(name, secret_values) or not (cleaned := clean_target_label(name)):
        return None
    # A double quote would let a page close the quoted name and forge a second sentence. The swap can
    # itself spell a registered secret, so what is persisted is checked again.
    display = cleaned.replace('"', "'")
    return None if target_label_hides_a_secret(display, secret_values) else display
