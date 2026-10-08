from __future__ import annotations

import random
import string

import pytest

from skyvern.forge.taskv3.field_commit import EntryMatch, compare_text, entry_outcome


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
