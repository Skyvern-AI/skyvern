"""Unit tests for the pure Task V3 target-label module (skyvern/forge/taskv3/target_label.py):
the floor vocabulary, the shape filter, the secret matcher, and their composition.
"""

from __future__ import annotations

import pytest

from skyvern.forge.taskv3.target_label import (
    TARGET_NAME_CAP,
    TARGET_VERBS,
    _canonical_for_secret_match,
    clean_target_label,
    compose_target_intention,
    name_looks_like_a_label,
    target_label_hides_a_secret,
)
from tests.unit.test_agent_task_v3 import _SECRET_BEARING_LABELS


def test_clean_target_label_strips_what_a_page_could_forge_a_timeline_row_with() -> None:
    # The captured name is page-authored and lands in a persisted row and a UI line: a newline could
    # forge a second line, and a bidi override could reverse the text around it.
    assert clean_target_label("Sign‮in⁩") == "Signin"
    assert clean_target_label("Continue\nto payment") == "Continue to payment"
    # U+061C (Arabic letter mark) is an invisible bidi control like the others.
    assert clean_target_label("Pay ؜100 now") == "Pay 100 now"
    assert clean_target_label("  Place   order  ") == "Place order"
    # A name is left as the page wrote it otherwise, accents and all -- including U+200C, which joins
    # letters in Persian and Hindi rather than being a control to strip (the download-notice scrub,
    # which this deliberately does NOT reuse, removes it and corrupts the word).
    assert clean_target_label("Créer un compte — étape 2") == "Créer un compte — étape 2"
    assert clean_target_label("می‌خواهم") == "می‌خواهم"
    assert clean_target_label("​ \n ") is None
    assert clean_target_label(None) is None
    assert clean_target_label(12) is None
    # A capture that REACHES the transport cap is dropped, not sliced. A slice would be page text
    # rather than a name, and -- the reason this is a hard rule -- secret scrubbing downstream
    # matches whole values, so a registered secret cut by the cap would no longer match its
    # registered form and would survive into a persisted, displayed row.
    assert len(clean_target_label("x" * (TARGET_NAME_CAP - 1)) or "") == TARGET_NAME_CAP - 1
    assert clean_target_label("x" * TARGET_NAME_CAP) is None
    assert clean_target_label("Session expired: " + "j" * (TARGET_NAME_CAP * 2)) is None
    # The cap is judged on the RAW length, before the strip. Otherwise a capture that WAS truncated
    # shrinks back under the threshold and is accepted: removable characters ahead of a credential
    # arrive at exactly the cap and clean down to a fragment that no longer matches the secret it
    # was cut from -- which is the one thing whole-value redaction can never catch.
    padded_fragment = "​" * (TARGET_NAME_CAP - 50) + "S" * 50
    assert len(padded_fragment) == TARGET_NAME_CAP
    assert clean_target_label(padded_fragment) is None


def test_display_cleaning_cannot_change_what_the_secret_check_sees() -> None:
    # The secret check runs on the RAW capture, upstream of `clean_target_label`. That ordering is
    # only SAFE because canonicalization strips a superset of what the display cleaner removes, so
    # the two see the same thing -- which is why swapping the order today is behaviour-preserving and
    # no end-to-end test can catch it. This pins the invariant itself: if the cleaner ever removes a
    # character the canonicalizer keeps, cleaning first would hide a credential from the check, and
    # this reds instead.
    for raw in (
        "Saved sk48​2913‍7765 ok",  # zero-width injected into the value
        "Saved sk4829‮137765 ok",  # bidi override injected into the value
        "mi‌xa",  # a joiner the DISPLAY cleaner deliberately keeps
        "line\nbreaks\tand   runs",
        "  leading and trailing  ",
        "Code123456accepted",
        # Long, but under the cap: a cleaner that ever TRUNCATES instead of rejecting would cut a
        # credential here into a shape the check no longer recognizes -- the exact bug shape this
        # ordering exists to survive.
        "Session note " + "j" * 300,
    ):
        cleaned = clean_target_label(raw)
        assert cleaned is not None, raw[:40]
        assert _canonical_for_secret_match(raw) == _canonical_for_secret_match(cleaned)


def test_secret_matcher_folds_case_before_stripping_the_expansion_it_produces() -> None:
    # `'İ'.casefold()` expands to `'i' + U+0307` (a combining dot above, category Mn). Folding BEFORE
    # the invisible-category strip means that Mn character is still in front of the strip pass and
    # gets removed with everything else a page could hide; folding LAST (the previous bug) leaves it
    # sitting between two ordinary letters and splits what should have been a substring match.
    assert target_label_hides_a_secret("Code SK8BOARD!CHİP99", {"sk8board!chip99"}) is True
    # Positive control, same call shape: an unrelated secret must not match regardless of the fold.
    assert target_label_hides_a_secret("Code SK8BOARD!CHİP99", {"unrelated-registered-secret"}) is False
    # U+0345 is a combining mark that casefolds into a letter (iota): stripping before the fold
    # would delete a letter of the credential rather than a mark.
    assert target_label_hides_a_secret("Saved passwordͅ ok", {"passwordι"}) is True


@pytest.mark.parametrize(
    ("rendered", "secret"),
    [
        # A zero-width space between a letter and its combining accent blocks composition, so a
        # composed canonical form would keep the page's copy decomposed and the secret's composed.
        pytest.param("Saved passe​́x", "passéx", id="accent-split-by-zero-width"),
        pytest.param("Saved ᄒ​ᅡᆫ글", "한글", id="hangul-jamo-split"),
    ],
)
def test_secret_matcher_sees_through_a_split_composition(rendered: str, secret: str) -> None:
    assert target_label_hides_a_secret(rendered, {secret}) is True
    assert target_label_hides_a_secret(rendered, {"unrelated"}) is False


def test_a_quote_in_the_name_cannot_forge_a_second_sentence() -> None:
    label = compose_target_intention("click", 'Go" button. Typed into "SSN', "button", set())
    assert label == "Clicked the \"Go' button. Typed into 'SSN\" button"
    assert label.count('"') == 2


def test_the_matcher_recognizes_a_registered_secret_in_an_encoded_form() -> None:
    # Base64 of "hunter". The shape filter rejects it too; this pins the matcher on its own.
    assert target_label_hides_a_secret("aHVudGVy", {"hunter"}) is True
    assert target_label_hides_a_secret("aHVudGVy", {"unrelated"}) is False


def test_the_quote_swap_cannot_spell_a_registered_secret() -> None:
    assert compose_target_intention("click", 'a"b"c', "button", {"a'b'c"}) == "Clicked a button"
    assert compose_target_intention("click", 'a"b"c', "button", {"unrelated"}) == "Clicked the \"a'b'c\" button"


@pytest.mark.parametrize(("rendered_label", "secret"), _SECRET_BEARING_LABELS)
def test_every_secret_bearing_label_is_rejected_by_shape_alone(rendered_label: str, secret: str) -> None:
    # The shape filter is the PRIMARY defense: it has to drop these with nothing in the secret set to
    # compare against, because a default (unmasked) run registers no secrets at all. `secret` is
    # unused here on purpose -- this asserts the shape filter alone is sufficient, not the matcher.
    del secret
    assert name_looks_like_a_label(rendered_label) is False


_SHAPE_REJECTED_NAMES = [
    pytest.param("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ9.c2ln", id="jwt"),
    pytest.param("4111111111111111", id="card-number"),
    pytest.param("4111 1111 1111 1111", id="card-number-spaced"),
    pytest.param("sk-live-51Habcdefghijkl", id="stripe-live-key"),
    pytest.param("sk_test_abcdef", id="stripe-test-key"),
    pytest.param("a3f5b9c2d4e6f8a1b3c5d7e9f1a3b5c7d9e1f3a5", id="hex-blob"),
    pytest.param("Code: 123456", id="otp"),
    pytest.param("hunter2", id="password"),
    pytest.param("ghp_abcdefghijklmnop", id="github-token"),
    pytest.param("AKIAIOSFODNN7EXAMPLE", id="aws-access-key"),
    pytest.param("jane.doe@example.com", id="email"),
    pytest.param("１２３４５６", id="fullwidth-digits"),
    pytest.param("12​34​56", id="digits-split-by-zero-width"),
    pytest.param("This label is deliberately longer than forty characters", id="over-max-chars"),
    # Each of these trips exactly one rule, so removing that rule alone reds its case.
    pytest.param("sk-abcdefghij", id="key-prefix-only"),
    pytest.param("password: hunter", id="keyword-assignment-only"),
    pytest.param("abcdefabcdef", id="letters-only-hex"),
    pytest.param("abcdefghijklmnopqrstu", id="long-letters-only-token"),
    # Rejected by default: no rule names these characters, the allowlist simply lacks them.
    pytest.param("p@ss word", id="unlisted-character"),
    pytest.param("Code aHVudGVy", id="random-case-word"),
    pytest.param("12-34-56", id="mostly-not-letters"),
    pytest.param("Signed in as jane.doe", id="dot-inside-a-word"),
    pytest.param("Bearer abcdefghijk", id="bearer-scheme"),
    pytest.param("https://a.co?sig = abcdefghijklmnop", id="reflowed-signed-url"),
    pytest.param("www.ex.co/reset", id="schemeless-url"),
    pytest.param("https ://a.co ?sig = abcdefghijklmnop", id="url-with-spaced-scheme"),
    pytest.param("PIN 1234", id="four-digit-pin"),
    pytest.param("Code 4829", id="short-code"),
    pytest.param("1 2 3 4", id="spaced-pin"),
]


@pytest.mark.parametrize("raw", _SHAPE_REJECTED_NAMES)
def test_shape_filter_rejects_credential_shaped_names(raw: str) -> None:
    assert name_looks_like_a_label(raw) is False


_SHAPE_ACCEPTED_NAMES = [
    "Sign In",
    "Continue to review",
    "Email address",
    "Confirm password",
    "Step 1 of 3",
    "Datenschutzerklärung",
    "Don't have an account?",
    "Phone (optional)",
    "Submit application",
    "Add to cart",
    "Page 2 of 10",
    "Save & close",
    # One lower-to-upper flip is a brand name, not random case.
    "Sign in with LinkedIn",
    "Continue with GitHub",
]


@pytest.mark.parametrize("raw", _SHAPE_ACCEPTED_NAMES)
def test_shape_filter_passes_ordinary_labels(raw: str) -> None:
    assert name_looks_like_a_label(raw) is True
    # Passing the shape filter alone is not the claim -- it must actually enrich the floor, with
    # nothing else in the way (no registered secret, click's own generic kind).
    assert compose_target_intention("click", raw, None, set()) == f'Clicked "{raw}"'


def test_floor_label_uses_only_the_tool_and_the_kind_never_page_text() -> None:
    # A captured kind with no name: the floor names the control's shape.
    assert compose_target_intention("click", None, "button", set()) == "Clicked a button"
    # A shape-rejected name still floors, but on the kind's own noun -- the raw text never appears.
    card_number = "4111 1111 1111 1111"
    label = compose_target_intention("type", card_number, "email", set())
    assert label == "Typed into an email field"
    assert card_number not in label
    # No capture at all (a failed dispatch): the tool's own default, generic for click/hover.
    assert compose_target_intention("click", None, None, set()) == "Clicked an element"
    # An unrecognized page-supplied kind falls through to the tool default and never leaks into text.
    unknown_kind_label = compose_target_intention("click", None, "sk-live-abc", set())
    assert unknown_kind_label == "Clicked an element"
    assert "sk-live" not in unknown_kind_label
    assert unknown_kind_label != "Click"


def test_a_failed_call_never_reads_as_if_it_succeeded() -> None:
    assert compose_target_intention("click", None, "button", set(), succeeded=False) == "Tried to click a button"
    assert compose_target_intention("type", None, None, set(), succeeded=False) == "Tried to type into a text field"
    assert compose_target_intention("click", None, "button", set()) == "Clicked a button"


def test_enriched_label_quotes_the_name_over_the_kinds_noun() -> None:
    assert compose_target_intention("click", "Sign In", "button", set()) == 'Clicked the "Sign In" button'
    assert compose_target_intention("type", "Email address", "email", set()) == 'Typed into the "Email address" field'
    # Dedupe: the captured name already ends with the noun, so it is not repeated.
    assert compose_target_intention("click", "Submit button", "button", set()) == 'Clicked the "Submit button"'
    # Only a whole trailing word counts as the noun.
    assert compose_target_intention("type", "Airfield", "textbox", set()) == 'Typed into the "Airfield" field'


def test_secret_matcher_floors_a_word_shaped_registered_secret() -> None:
    # "correct horse battery" is ordinary prose -- the shape filter alone lets it through -- so only
    # the secret matcher (layer 3) can catch it once it is REGISTERED for this run.
    secret_values = {"correct horse battery"}
    assert name_looks_like_a_label("correct horse battery") is True
    assert compose_target_intention("click", "correct horse battery", None, secret_values) == "Clicked an element"
    # POSITIVE CONTROL, same secret set: an unrelated name still enriches.
    assert (
        compose_target_intention("click", "Continue to review", None, secret_values) == 'Clicked "Continue to review"'
    )


def test_target_verbs_never_cover_a_tool_with_no_single_named_target() -> None:
    assert "press_key" not in TARGET_VERBS
    assert "navigate" not in TARGET_VERBS
