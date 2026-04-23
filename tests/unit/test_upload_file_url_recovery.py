"""Tests for URL recovery when the LLM subtly corrupts a user-provided upload URL.

Covers the hallucination-check bypass in ``handle_upload_file_action``: long
pre-signed URLs (S3, Supabase Storage) occasionally round-trip through the LLM
with one or two garbled characters, which defeats the strict substring match
against ``navigation_goal`` / ``navigation_payload``. The helper must recover
the verbatim URL from the user-supplied text when the corruption is bounded,
and must *not* substitute unrelated URLs.
"""

from skyvern.webeye.actions.handler import _find_similar_url_in_text

SUPABASE_URL = (
    "https://abc123.supabase.co/storage/v1/object/sign/resumes/resume.pdf"
    "?token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1cmwiOiJyZXN1bWVzL3Jlc3VtZS5wZGYiLCJpYXQiOjE3MTA0MDAwMDAsImV4cCI6MTcxMDQwMzYwMH0"
    ".abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234"
)


def test_returns_verbatim_url_when_present():
    goal = f"Upload the resume at {SUPABASE_URL} to the application form."
    assert _find_similar_url_in_text(SUPABASE_URL, goal) == SUPABASE_URL


def test_recovers_single_character_corruption_in_token():
    # Flip one character deep inside the JWT — a typical LLM copy error.
    corrupted = SUPABASE_URL[:-10] + "X" + SUPABASE_URL[-9:]
    goal = f"Please upload the resume from {SUPABASE_URL}"
    assert _find_similar_url_in_text(corrupted, goal) == SUPABASE_URL


def test_recovers_single_case_flip_in_token():
    # A single case flip inside the JWT still recovers the verbatim URL.
    corrupted = SUPABASE_URL.replace("abcdef1234567890", "Abcdef1234567890", 1)
    goal = f"Upload: {SUPABASE_URL}"
    assert _find_similar_url_in_text(corrupted, goal) == SUPABASE_URL


def test_returns_none_when_no_similar_url():
    goal = "Upload a file from https://example.com/cover_letter.pdf"
    assert _find_similar_url_in_text(SUPABASE_URL, goal) is None


def test_returns_none_when_text_has_no_urls():
    assert _find_similar_url_in_text(SUPABASE_URL, "no url in this text") is None


def test_returns_none_when_inputs_empty():
    assert _find_similar_url_in_text("", SUPABASE_URL) is None
    assert _find_similar_url_in_text(SUPABASE_URL, "") is None


def test_rejects_different_path_even_with_similar_query():
    # Same host and tokens but different object path — never substitute, this
    # is a different file.
    other = SUPABASE_URL.replace("/resume.pdf", "/cover_letter.pdf", 1)
    goal = f"Upload: {other}"
    assert _find_similar_url_in_text(SUPABASE_URL, goal) is None


def test_rejects_different_host():
    other = SUPABASE_URL.replace("abc123.supabase.co", "evil.example.com", 1)
    goal = f"Upload: {other}"
    assert _find_similar_url_in_text(SUPABASE_URL, goal) is None


def test_picks_best_match_among_multiple_urls():
    resume = SUPABASE_URL
    cover = resume.replace("/resume.pdf", "/cover_letter.pdf", 1)
    corrupted_resume = resume[:-5] + "ZZZZZ"
    goal = f"Resume: {resume} Cover: {cover}"
    # corrupted_resume shares path with `resume`, not with `cover`, so we recover the resume.
    assert _find_similar_url_in_text(corrupted_resume, goal) == resume


def test_strips_trailing_punctuation_around_url():
    # URLs embedded in prose often have trailing punctuation like `.` or `,`.
    goal = f"Upload the resume at {SUPABASE_URL}. Then click submit."
    assert _find_similar_url_in_text(SUPABASE_URL, goal) == SUPABASE_URL


def test_normalizes_trailing_punctuation_on_candidate():
    # The LLM sometimes includes trailing punctuation in its echoed URL.
    goal = f"Upload the resume at {SUPABASE_URL}"
    assert _find_similar_url_in_text(SUPABASE_URL + ".", goal) == SUPABASE_URL
    assert _find_similar_url_in_text(SUPABASE_URL + ")", goal) == SUPABASE_URL


def test_recovers_across_hostname_casing_difference():
    # DNS hostnames are case-insensitive; a candidate with different host casing
    # should still recover the verbatim URL.
    upper_host_candidate = SUPABASE_URL.replace("abc123.supabase.co", "ABC123.SUPABASE.CO", 1)
    goal = f"Resume URL: {SUPABASE_URL}"
    assert _find_similar_url_in_text(upper_host_candidate, goal) == SUPABASE_URL


def test_explicit_port_treated_as_different_from_implicit():
    url_with_port = "https://abc123.supabase.co:443/storage/v1/resume.pdf?token=abc"
    goal = f"Upload: {url_with_port}"
    # Same origin, no port on candidate — still treated as a mismatch because
    # explicit 443 is distinct from an empty port in urlparse. This guards the
    # assumption downstream code encodes about port-sensitivity.
    result = _find_similar_url_in_text(
        "https://abc123.supabase.co/storage/v1/resume.pdf?token=abd",
        goal,
    )
    assert result is None


def test_recovers_single_edit_on_very_short_url():
    # Floor on max edit distance: even a 12-char URL must allow at least one edit,
    # otherwise typical LLM character flips in short URLs would never recover.
    short_url = "https://a.co"
    corrupted = "https://A.co"  # one case flip in hostname
    goal = f"Upload from {short_url}"
    assert _find_similar_url_in_text(corrupted, goal) == short_url


def test_rejects_close_edit_distance_cross_origin():
    # Hostname differs by exactly 1 char ("abc123" vs "abd123") — within any
    # reasonable edit-distance bound. Origin-key gate must still reject.
    attacker = SUPABASE_URL.replace("abc123.supabase.co", "abd123.supabase.co", 1)
    goal = f"Upload: {attacker}"
    assert _find_similar_url_in_text(SUPABASE_URL, goal) is None
