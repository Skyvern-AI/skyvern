from __future__ import annotations

import random
import string

import pytest

from skyvern.forge.taskv3.field_commit import (
    ALL_TIERS,
    EntryMatch,
    Pick,
    Refusal,
    Tier,
    compare_text,
    entry_outcome,
    match_choice,
    nearest,
    with_tier_note,
)
from skyvern.forge.taskv3.loop import ToolResult


@pytest.mark.parametrize(
    ("typed", "held", "before", "match"),
    [
        ("hello world", "hello world", None, EntryMatch.SAME),
        ("hello world", " hello  world ", None, EntryMatch.SAME),
        ("5551234567", "(555) 123-4567", None, EntryMatch.REFORMATTED),
        ("2026-09-18", "2026-09-18", None, EntryMatch.SAME),
        ("Main Street", "MAIN STREET", None, EntryMatch.REFORMATTED),
        ("1500", "$1,500", None, EntryMatch.REFORMATTED),
        ("1500.5", "1,500.50", None, EntryMatch.REFORMATTED),
        ("abc", "", None, EntryMatch.NOT_HELD),
        ("abc", "old value", "old value", EntryMatch.NOT_HELD),
        ("12", "123", None, EntryMatch.DIFFERENT),
        ("x", "old valuex", "old value", EntryMatch.DIFFERENT),
        ("abcdef", "abc", None, EntryMatch.DIFFERENT),
        ("+1 5551234567", "5551234567", None, EntryMatch.DIFFERENT),
        ("0123", "123", None, EntryMatch.DIFFERENT),
        ("abc", None, None, None),
        ("123456789", "123-45-6789", None, EntryMatch.REFORMATTED),
        ("John@X.com", "john@x.com", None, EntryMatch.REFORMATTED),
        ("75000.00", "7500000", None, EntryMatch.DIFFERENT),
        ("-5", "5", None, EntryMatch.DIFFERENT),
        ("12/1/2024", "12/12/024", None, EntryMatch.DIFFERENT),
        ("a.b@c.com", "ab@c.com", None, EntryMatch.DIFFERENT),
        ("3:30", "330", None, EntryMatch.DIFFERENT),
        ("+1 555 123 4567", "+1 (555) 123-4567", None, EntryMatch.REFORMATTED),
        ("1500", "-1500", None, EntryMatch.DIFFERENT),
        ("9/5/2026", "09/05/2026", None, EntryMatch.REFORMATTED),
        ("A-7", "A-007", None, EntryMatch.DIFFERENT),
        ("AB-07", "AB-7", None, EntryMatch.DIFFERENT),
        ("+15551234567", "+1 (555) 123-4567", None, EntryMatch.REFORMATTED),
        ("12.5", "12.50", None, EntryMatch.REFORMATTED),
        ("(1500)", "-1500", None, EntryMatch.DIFFERENT),
        ("1,500", "1500", None, EntryMatch.REFORMATTED),
        ("   ", "", None, EntryMatch.NOT_HELD),
    ],
    ids=[
        "exact",
        "whitespace",
        "mask",
        "iso_date",
        "case",
        "currency",
        "decimal_grouping",
        "emptied",
        "unchanged",
        "residue",
        "appended_to_prior",
        "capped",
        "dropped_prefix",
        "leading_zero_identifier",
        "unreadable",
        "id_mask",
        "email_case",
        "decimal_point_dropped",
        "sign_dropped",
        "date_regrouped",
        "email_dot_dropped",
        "time_colon_dropped",
        "plus_prefixed_phone_mask",
        "sign_added",
        "zero_padded_date_parts",
        "padded_identifier",
        "unpadded_identifier",
        "e164_phone_mask",
        "decimal_trailing_zero",
        "sign_added_to_unsigned_text",
        "typed_grouping_stripped",
        "whitespace_trimmed_to_empty",
    ],
)
def test_compare_text(typed: str, held: str | None, before: str | None, match: EntryMatch | None) -> None:
    assert compare_text(typed, held, before) is match


def test_compare_text_never_accepts_extra_or_missing_alphanumerics() -> None:
    # The false successes a looser rule would accept: anything that adds, drops or changes a letter or digit.
    rng = random.Random(7)
    alphabet = string.ascii_letters + string.digits
    for _ in range(500):
        typed = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 12)))
        extra = typed + rng.choice(alphabet)
        assert compare_text(typed, extra) is EntryMatch.DIFFERENT
        assert compare_text(extra, typed) is EntryMatch.DIFFERENT
        separated = "-".join(typed)
        assert compare_text(typed, separated) in (EntryMatch.SAME, EntryMatch.REFORMATTED)
        # A sign or a decimal point the page added or dropped changes a number in both directions.
        number = "".join(rng.choice(string.digits[1:]) for _ in range(rng.randint(2, 7)))
        cut = rng.randint(1, len(number) - 1)
        for changed in ("-" + number, "+" + number, number[:cut] + "." + number[cut:]):
            assert compare_text(number, changed) is EntryMatch.DIFFERENT, (number, changed)
            assert compare_text(changed, number) is EntryMatch.DIFFERENT, (changed, number)


def test_an_exact_compare_never_folds_a_secret() -> None:
    assert compare_text("Ab C", "ab c") is EntryMatch.REFORMATTED
    assert compare_text("Ab C", "ab c", exact=True) is EntryMatch.DIFFERENT
    assert compare_text("Ab C", "Ab C", exact=True) is EntryMatch.SAME
    assert compare_text("Ab C", "", exact=True) is EntryMatch.NOT_HELD


def test_entry_outcome_never_echoes_a_value_it_was_not_given() -> None:
    for match in (EntryMatch.REFORMATTED, EntryMatch.DIFFERENT):
        assert "'" not in entry_outcome("#f", match, shown=None).content
    assert "'4417'" in entry_outcome("#f", EntryMatch.DIFFERENT, shown="4417").content


def _rows(*texts: str) -> list[dict[str, object]]:
    return [{"n": i, "text": t} for i, t in enumerate(texts, 1)]


_TYPEAHEAD = ALL_TIERS - {Tier.PREFIX}


def test_match_choice_menu_matrix() -> None:
    def pick(value: str, rows: list[dict[str, object]]) -> int | None:
        choice = match_choice(value, rows, complete=True)
        return choice.n if isinstance(choice, Pick) else None

    opts = _rows("Analytics", "Engineering", "People Operations")
    assert pick("Analytics", opts) == 1
    assert pick("  analytics ", opts) == 1
    assert pick("PEOPLE   OPERATIONS", opts) == 3
    # A unique forward word-prefix commits on a whole list; the reverse direction and a bare substring never do.
    assert pick("People", opts) == 3
    eeo = _rows("Yes", "No", "Decline to self-identify")
    assert pick("Decline", eeo) == 3
    assert pick("People Operations Team", opts) is None
    assert pick("New York", _rows("New", "Newark")) is None
    assert pick("Prefer not to answer", eeo) is None
    assert pick("Eng", opts) is None
    assert pick("Masters Degree", _rows("Master's Degree", "PhD")) == 1
    assert pick("Legal", opts) is None
    assert pick("", opts) is None
    assert pick("Yes", _rows("Yes", "Yes, I consent", "No")) == 1
    assert pick("United States", _rows("United States Minor", "United States Major")) is None
    assert pick("Analytics", [{"n": None, "text": "Analytics"}, {"n": 5, "text": "Analytics"}]) == 5
    assert pick("Yes", _rows("Yes, I consent", "No")) == 1
    assert pick("Yes", _rows("Yes/No", "Maybe")) is None


@pytest.mark.parametrize(
    ("value", "texts", "n", "tier"),
    [
        ("United States", ["\U0001f1fa\U0001f1f8 United States +1"], 1, Tier.PRIMARY),
        # The decorated row is the value's main label; the longer country only starts with it, a worse rank.
        (
            "United States",
            ["\U0001f1fa\U0001f1f8 United States +1", "\U0001f1fa\U0001f1f2 United States Minor Outlying Islands +1"],
            1,
            Tier.PRIMARY,
        ),
        ("United Kingdom", ["\U0001f1ec\U0001f1e7 United Kingdom +44"], 1, Tier.PRIMARY),
        ("Canada", ["Canada (+1)"], 1, Tier.PRIMARY),
        ("10023", ["10023(Example Co)"], 1, Tier.PRIMARY),
        ("Full-time", ["Full-time - Permanent"], 1, Tier.PRIMARY),
        ("Accounts", ["Account"], 1, Tier.STEM),
    ],
)
def test_match_choice_commits_a_lone_decorated_row_on_a_whole_typed_list(
    value: str, texts: list[str], n: int, tier: Tier
) -> None:
    assert match_choice(value, _rows(*texts), complete=True, allow=_TYPEAHEAD) == Pick(n, texts[n - 1], tier)
    # The same hit never commits from part of a list, or from rows a shortened query rendered.
    assert match_choice(value, _rows(*texts), complete=False, allow=_TYPEAHEAD).reason == "incomplete"
    assert match_choice(value, _rows(*texts), complete=True, query_full=False).reason == "reduced_query"


@pytest.mark.parametrize(
    ("value", "texts"),
    [
        ("United States", ["+1"]),
        # A different ID is a different record, an unspaced dash is part of the label, and a comma heads a hierarchy.
        ("1002", ["10023 - Example Co"]),
        ("Full", ["Full-time - Permanent"]),
        ("Springfield, USA", ["Springfield, Illinois, USA"]),
        # The row side only: a row shorter than the value never stands for it.
        ("Canada (+1)", ["Canada"]),
        ("I accept", ["I agree", "I do not agree"]),
    ],
)
def test_match_choice_refuses_and_offers_rows_that_are_not_the_value(value: str, texts: list[str]) -> None:
    choice = match_choice(value, _rows(*texts), complete=True)
    assert isinstance(choice, Refusal) and choice.reason == "none", choice
    if value != "United States":
        assert nearest(value, _rows(*texts))[0].row["text"] == texts[0]


@pytest.mark.parametrize(
    ("value", "texts"),
    [
        # A leading sign is part of the label, never decoration.
        ("1 year", ["< 1 year", "1-2 years", "2-5 years"]),
        ("18", ["< 18", "18-24", "25-34"]),
        ("10 years", ["> 10 years", "5-10 years", "1-5 years"]),
        ("Other", ["~ Other", "Something else"]),
        # A trailing "+N" with no flag beside it is a count, not a dialing code.
        ("Employee", ["Employee +1", "Employee Only"]),
        ("Employee", ["Employee +1"]),
        # Only a flag makes "+N" a dialing code; any other pictograph is decoration and nothing more.
        ("Employee", ["\U0001f465 Employee +1", "Employee Only"]),
        ("Gold", ["\u2b50 Gold +2"]),
    ],
)
def test_match_choice_never_commits_a_row_whose_sign_or_count_changes_the_value(value: str, texts: list[str]) -> None:
    for allow in (_TYPEAHEAD, ALL_TIERS):
        choice = match_choice(value, _rows(*texts), complete=True, allow=allow)
        assert not (isinstance(choice, Pick) and choice.tier is Tier.PRIMARY), (allow, choice)
    assert not isinstance(match_choice(value, _rows(*texts), complete=True, allow=_TYPEAHEAD), Pick)


def test_match_choice_names_every_contender_of_one_rank() -> None:
    both = match_choice("Springfield", _rows("Springfield, IL", "Springfield, MO"), complete=True)
    assert both == Refusal("ambiguous", tuple(_rows("Springfield, IL", "Springfield, MO")))
    # A main label and a plural are the same distance from the value, so neither outranks the other.
    rival = match_choice("Account", _rows("Account (closed)", "Accounts"), complete=True)
    assert isinstance(rival, Refusal) and rival.reason == "ambiguous" and len(rival.contenders) == 2


def test_match_choice_refuses_exact_twins_before_any_lower_tier() -> None:
    rows = [{"n": 1, "text": "Main", "val": "a"}, {"n": 2, "text": "Main", "val": "b"}, {"n": 3, "text": "Main Office"}]
    assert match_choice("Main", rows, complete=True, collapse=True).reason == "identical"


def test_a_click_opened_list_with_main_labels_switched_off_still_commits_a_lone_prefix_row() -> None:
    # The kill switch must leave a click-opened list where main left it: a lone row starting with the value commits.
    rows = [{"n": 1, "text": "Other (please specify)"}, {"n": 2, "text": "Yes"}]
    assert match_choice("Other", rows, complete=True, allow=ALL_TIERS - {Tier.PRIMARY}) == Pick(
        1, "Other (please specify)", Tier.PREFIX
    )


def test_match_choice_counts_the_leaves_of_one_row_once() -> None:
    # A row whose accessible name is the value, rendered as a flag leaf and a dial-code leaf.
    leaves = [{"n": 4, "text": "\U0001f1fa\U0001f1f8", "label": "United States", "unit": 4}]
    leaves.append({"n": 5, "text": "+1", "label": "United States", "unit": 4})
    assert match_choice("United States", leaves, complete=True) == Pick(4, leaves[0]["text"], Tier.PRIMARY)
    # Two controls inside one declared row, each with its own label, are two contenders.
    controls = [
        {"n": 7, "text": "Full-time - Permanent", "unit": 7},
        {"n": 8, "text": "Full-time - Contract", "unit": 7},
    ]
    assert match_choice("Full-time", controls, complete=True).reason == "ambiguous"


def test_a_typed_search_offers_a_row_that_only_starts_with_the_value_first() -> None:
    rows = _rows("Springdale, AR", "Springfield, Sangamon, IL")
    choice = match_choice("Springfield", rows, complete=True, allow=_TYPEAHEAD)
    assert isinstance(choice, Refusal) and choice.reason == "none"
    offers = nearest("Springfield", rows)
    assert offers[0].row["text"] == "Springfield, Sangamon, IL" and offers[0].tier is Tier.PREFIX


def test_a_refused_paraphrase_commits_once_the_offered_label_is_sent() -> None:
    rows = _rows("I do not agree", "I agree")
    assert match_choice("I accept", rows, complete=True).reason == "none"
    offered = [o.row["text"] for o in nearest("I accept", rows)]
    assert sorted(offered) == ["I agree", "I do not agree"]
    assert match_choice(offered[offered.index("I agree")], rows, complete=True) == Pick(2, "I agree", Tier.EXACT)


def test_a_non_exact_commit_names_its_tier() -> None:
    ok = ToolResult.ok("selected 'United States +1' for #cc")
    noted = with_tier_note(ok, Tier.PRIMARY, "United States")
    assert noted.content.endswith("not an exact match: its main label is 'United States'"), noted.content
    assert noted.data == {"choice": {"tier": "primary"}}
    assert with_tier_note(ok, Tier.EXACT, "United States") is ok
    refused = ToolResult.error("no", error_class="no_matching_row")
    assert with_tier_note(refused, Tier.PRIMARY, "x") is refused
