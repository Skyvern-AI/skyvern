"""Canonical form field categories for keyword-based matching.

Instead of growing FIELD_MAP with every label variant, this module defines
~20 canonical categories with keyword lists.  ``match_field_to_category``
scores a page field label against all categories and returns the best match
(or ``None``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Stop words stripped during normalization
_STOP_WORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "your",
        "you",
        "please",
        "enter",
        "provide",
        "what",
        "is",
        "are",
        "do",
        "does",
        "of",
        "for",
        "to",
        "in",
        "on",
        "at",
        "by",
        "with",
        "this",
        "that",
        "us",
        "our",
        "we",
        "my",
        "me",
        "i",
    }
)

_NON_ALPHA = re.compile(r"[^a-z0-9\s]")


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, remove stop words.

    Hyphens are replaced with spaces (so "e-mail" → "e mail") while other
    punctuation is removed entirely (so "what's" → "whats").
    """
    text = text.lower().replace("-", " ")
    text = _NON_ALPHA.sub("", text)
    return " ".join(w for w in text.split() if w not in _STOP_WORDS)


@dataclass(frozen=True)
class CanonicalCategory:
    name: str
    keywords: frozenset[str]
    negative_keywords: frozenset[str] = field(default_factory=frozenset)
    param: str | None = None
    action: str = "fill"
    prompt: str = ""
    # Maps semantic intent (e.g. "no") to common option label substrings.
    # Used by _resolve_fallback_value to match planner output like "No" to
    # actual option text like "I am not a protected veteran".
    value_mappings: tuple[tuple[str, tuple[str, ...]], ...] = ()


# ---------------------------------------------------------------------------
# Category registry
# ---------------------------------------------------------------------------

CANONICAL_CATEGORIES: list[CanonicalCategory] = [
    CanonicalCategory(
        name="full_name",
        keywords=frozenset({"full name", "name"}),
        negative_keywords=frozenset(
            {
                "company",
                "employer",
                "school",
                "university",
                "linkedin",
                "user",
                "login",
                "first",
                "last",
                "surname",
                "family",
            }
        ),
        param="full_name",
        action="fill",
        prompt="Fill the applicant's full name",
    ),
    CanonicalCategory(
        name="first_name",
        keywords=frozenset({"first name"}),
        negative_keywords=frozenset({"company", "employer"}),
        param="first_name",
        action="fill",
        prompt="Fill the applicant's first name",
    ),
    CanonicalCategory(
        name="last_name",
        keywords=frozenset({"last name", "surname", "family name"}),
        negative_keywords=frozenset({"company", "employer"}),
        param="last_name",
        action="fill",
        prompt="Fill the applicant's last name",
    ),
    CanonicalCategory(
        name="email",
        keywords=frozenset({"email", "e-mail", "email address"}),
        param="email",
        action="fill",
        prompt="Fill the applicant's email address",
    ),
    CanonicalCategory(
        name="phone",
        keywords=frozenset({"phone", "telephone", "mobile", "phone number", "cell"}),
        param="phone",
        action="fill",
        prompt="Fill the applicant's phone number",
    ),
    CanonicalCategory(
        name="resume",
        keywords=frozenset({"resume", "cv", "curriculum"}),
        param="resume",
        action="upload_file",
        prompt="Upload the applicant's resume",
    ),
    CanonicalCategory(
        name="current_location",
        keywords=frozenset({"location", "city", "based", "current location", "where"}),
        negative_keywords=frozenset({"job", "office", "company", "position", "role", "birth"}),
        param="current_location",
        action="fill_autocomplete",
        prompt="Fill the applicant's current location",
    ),
    CanonicalCategory(
        name="current_company",
        keywords=frozenset({"current company", "employer", "company name", "where do you work", "current employer"}),
        negative_keywords=frozenset({"location", "city"}),
        param="current_company",
        action="fill",
        prompt="Fill the applicant's current company",
    ),
    CanonicalCategory(
        name="linkedin",
        keywords=frozenset({"linkedin", "linkedin url", "linkedin profile"}),
        param="linkedin",
        action="fill",
        prompt="Fill the applicant's LinkedIn URL",
    ),
    CanonicalCategory(
        name="portfolio",
        keywords=frozenset({"portfolio", "website", "github", "personal site", "personal url"}),
        negative_keywords=frozenset({"linkedin"}),
        param="portfolio",
        action="fill",
        prompt="Fill the applicant's portfolio or website URL",
    ),
    CanonicalCategory(
        name="cover_letter",
        keywords=frozenset({"cover letter", "why interested", "motivation", "additional information", "anything else"}),
        action="fill",
        prompt="Write a brief, professional cover letter (2-3 sentences)",
    ),
    CanonicalCategory(
        name="salary_expectation",
        keywords=frozenset({"salary", "compensation", "pay", "desired salary", "expected salary"}),
        action="fill",
        prompt="Fill the applicant's salary expectation",
    ),
    CanonicalCategory(
        name="work_authorization",
        keywords=frozenset({"authorized", "legally", "sponsorship", "visa", "work permit", "authorization"}),
        action="select",
        prompt="Answer the work authorization question based on applicant context",
        value_mappings=(
            ("yes", ("yes", "authorized", "i am authorized", "legally authorized")),
            ("no", ("no", "not authorized", "i am not authorized", "require sponsorship")),
        ),
    ),
    CanonicalCategory(
        name="start_date",
        keywords=frozenset({"start date", "availability", "when can you start", "available", "earliest start"}),
        negative_keywords=frozenset({"graduation", "salary", "end"}),
        action="fill",
        prompt="Fill the applicant's start date or availability",
    ),
    CanonicalCategory(
        name="years_experience",
        keywords=frozenset({"years experience", "years of experience", "how long", "experience level"}),
        action="fill",
        prompt="Fill the applicant's years of experience",
    ),
    CanonicalCategory(
        name="education_level",
        keywords=frozenset({"degree", "education", "university", "school", "highest degree"}),
        negative_keywords=frozenset({"graduation", "year"}),
        action="fill",
        prompt="Fill the applicant's education level or degree",
    ),
    CanonicalCategory(
        name="graduation_year",
        keywords=frozenset({"graduation", "grad year", "year graduated", "graduation date"}),
        action="fill",
        prompt="Fill the applicant's graduation year",
    ),
    CanonicalCategory(
        name="gender",
        keywords=frozenset({"gender", "sex", "identify", "gender identity"}),
        action="select",
        prompt="Select the applicant's gender based on applicant context",
        value_mappings=(
            ("male", ("man", "male", "he", "him")),
            ("female", ("woman", "female", "she", "her")),
            ("nonbinary", ("non-binary", "nonbinary", "non binary", "genderqueer")),
            ("decline", ("decline", "prefer not", "not to say", "don't wish")),
        ),
    ),
    CanonicalCategory(
        name="race_ethnicity",
        keywords=frozenset({"race", "ethnicity", "ethnic", "racial"}),
        action="select",
        prompt="Select the applicant's race/ethnicity based on applicant context",
    ),
    CanonicalCategory(
        name="veteran_status",
        keywords=frozenset({"veteran", "military", "served", "protected veteran"}),
        action="select",
        prompt="Select the applicant's veteran status based on applicant context",
        value_mappings=(
            ("no", ("i am not a protected veteran", "i am not a veteran", "not a veteran", "no")),
            ("yes", ("i am a protected veteran", "i am a veteran", "veteran", "yes")),
            ("decline", ("i don't wish to answer", "decline to self-identify", "i prefer not to answer", "prefer not")),
        ),
    ),
    CanonicalCategory(
        name="disability",
        keywords=frozenset({"disability", "accommodation", "impairment", "disabled"}),
        action="select",
        prompt="Select the applicant's disability status based on applicant context",
        value_mappings=(
            ("no", ("i do not have a disability", "i don't have a disability", "no, i don't", "no disability", "no")),
            ("yes", ("yes, i have a disability", "i have a disability", "yes")),
            ("decline", ("i don't wish to answer", "decline to self-identify", "prefer not to answer", "prefer not")),
        ),
    ),
    CanonicalCategory(
        name="referral_source",
        keywords=frozenset(
            {"how did you hear", "referral", "referral source", "where did you find", "how did you find"}
        ),
        negative_keywords=frozenset({"salary", "visa", "location"}),
        action="select",
        prompt="Select or fill the referral source (e.g. 'Other')",
    ),
]

_CATEGORY_BY_NAME: dict[str, CanonicalCategory] = {c.name: c for c in CANONICAL_CATEGORIES}


def get_category(name: str) -> CanonicalCategory | None:
    """Look up a canonical category by name."""
    return _CATEGORY_BY_NAME.get(name)


def match_field_to_category(field_label: str) -> CanonicalCategory | None:
    """Score *field_label* against all canonical categories and return the best match.

    Returns ``None`` when no category matches (i.e. the field is truly custom).

    Matching algorithm:
    1. Normalize the label (lowercase, strip punctuation, remove stop words).
    2. For each category, check negative keywords first (any hit → skip).
    3. Score by counting how many of the category's keywords (also normalized)
       appear in the normalized label as substrings.
    4. Return the category with the highest score (minimum 1 keyword hit).
    """
    normalized = _normalize(field_label)
    if not normalized:
        return None

    best: CanonicalCategory | None = None
    best_score = 0

    for cat in CANONICAL_CATEGORIES:
        # Check negative keywords on the *original* (lowered) label so
        # stop-word removal doesn't hide exclusion terms.
        label_lower = field_label.lower()
        if any(neg in label_lower for neg in cat.negative_keywords):
            continue

        score = 0
        for kw in cat.keywords:
            # Normalize the keyword too so multi-word keywords with stop words
            # (e.g. "how did you hear") still match normalized labels.
            norm_kw = _normalize(kw)
            if norm_kw and norm_kw in normalized:
                score += 1

        if score > best_score:
            best_score = score
            best = cat

    return best
