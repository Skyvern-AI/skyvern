"""Executes the published webhook-signature verifiers against real Skyvern signatures.

The fixtures come from generate_skyvern_webhook_signature, the same function that signs
outbound webhooks and TOTP requests, so a snippet that passes here matches server behavior.
"""

import re
from pathlib import Path

import pytest

from skyvern.forge.sdk.core.security import generate_skyvern_webhook_signature

pytestmark = pytest.mark.boundary

REPO_ROOT = Path(__file__).resolve().parents[2]
SNIPPET = REPO_ROOT / "docs/snippets/webhook-signature-verification.mdx"

API_KEY = "sk_docs_snippet_test_key"
PAYLOAD = {"run_id": "tsk_1", "status": "completed", "step_count": 3}
_SIGNED = generate_skyvern_webhook_signature(payload=PAYLOAD, api_key=API_KEY)
RAW_BODY = _SIGNED.signed_payload.encode("utf-8")
GENUINE = _SIGNED.headers["x-skyvern-signature"]

# What an attacker can produce unaided if a verifier signs with an unset key: the empty
# key is public, so this is a valid signature for anyone who guesses the key is missing.
EMPTY_KEY_FORGED = generate_skyvern_webhook_signature(payload=PAYLOAD, api_key="").headers["x-skyvern-signature"]

# (label, signature, must_be_accepted)
CASES = [
    ("genuine", GENUINE, True),
    ("forged_same_length", "a" * len(GENUINE), False),
    ("forged_off_by_one_char", "b" + GENUINE[1:], False),
    ("forged_with_empty_key", EMPTY_KEY_FORGED, False),
    ("empty", "", False),
    ("missing", None, False),
    ("non_ascii", "é" * len(GENUINE), False),
]


def _code_block(language: str) -> str:
    body = SNIPPET.read_text(encoding="utf-8")
    match = re.search(rf"^```{language}\b[^\n]*\n(.*?)^```$", body, re.M | re.S)
    assert match, f"no ```{language} block in {SNIPPET}"
    return match.group(1)


def _extract(language: str, pattern: str) -> str:
    block = _code_block(language)
    match = re.search(pattern, block, re.M | re.S)
    assert match, f"no verifier function found in the {language} snippet"
    return match.group(0)


def _assert_verdicts(verdicts: dict[str, bool]) -> None:
    for label, _signature, must_accept in CASES:
        assert label in verdicts, f"driver returned no verdict for {label}"
        assert verdicts[label] is must_accept, (
            f"documented verifier {'rejected the genuine' if must_accept else 'ACCEPTED a bad'} "
            f"signature for case {label!r}"
        )


def test_python_snippet_rejects_forged_and_missing_signatures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKYVERN_API_KEY", API_KEY)
    key_setup = _extract("python", r"^SKYVERN_API_KEY = [^\n]*$")
    source = _extract("python", r"^def verify_skyvern_signature\b.*?\n(?=^\S|\Z)")

    namespace: dict = {}
    exec(f"import hashlib\nimport hmac\nimport os\n\n{key_setup}\n\n{source}", namespace)
    verify = namespace["verify_skyvern_signature"]

    _assert_verdicts({label: verify(RAW_BODY, signature) for label, signature, _ in CASES})


# A discarded timingSafeEqual call and a timing-unsafe equality compare: the two shapes
# that made the published verifiers permissive. Neither may reappear in any docs page.
DISCARDED_TIMING_SAFE_EQUAL = re.compile(r"^\s*(?:crypto\.)?timingSafeEqual\(", re.M)
UNSAFE_SIGNATURE_COMPARE = re.compile(
    r"signature[^\n=!<>]*={2,3}[^\n]*expected|expected[^\n=!<>]*={2,3}[^\n]*signature"
)


@pytest.mark.parametrize("doc", sorted(REPO_ROOT.joinpath("docs").rglob("*.mdx")), ids=lambda p: p.name)
def test_docs_contain_no_permissive_signature_check(doc: Path) -> None:
    text = doc.read_text(encoding="utf-8")
    assert not DISCARDED_TIMING_SAFE_EQUAL.search(text), (
        f"{doc.relative_to(REPO_ROOT)} calls timingSafeEqual as a statement; its return value must be used"
    )
    assert not UNSAFE_SIGNATURE_COMPARE.search(text), (
        f"{doc.relative_to(REPO_ROOT)} compares a signature with ==; use a constant-time comparison"
    )
