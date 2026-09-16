"""Executes the published webhook-signature verifiers against real Skyvern signatures.

The fixtures come from generate_skyvern_webhook_signature, the same function that signs
outbound webhooks and TOTP requests, so a snippet that passes here matches server behavior.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import textwrap
from collections.abc import Callable
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

SPEC = json.dumps({"rawBody": base64.b64encode(RAW_BODY).decode(), "cases": [[c[0], c[1]] for c in CASES]})


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


def _run_driver(cmd: list[str], workdir: Path, api_key: str | None = API_KEY) -> dict[str, bool] | None:
    """Returns the driver's verdicts, or None when it refused to run without a key."""
    env = {k: v for k, v in os.environ.items() if k != "SKYVERN_API_KEY"}
    if api_key is not None:
        env["SKYVERN_API_KEY"] = api_key

    result = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=120, env=env)
    if api_key is None and result.returncode != 0:
        return None
    assert result.returncode == 0, f"driver failed:\n{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def _require_runtime(name: str) -> None:
    # Skipped rather than failed when absent: this file is synced to the OSS repo, whose
    # CI does not install either toolchain. test_docs_contain_no_permissive_signature_check
    # is pure Python and always runs, so both original defects stay covered regardless.
    if shutil.which(name) is None:
        pytest.skip(f"{name} not installed")


def _typescript_driver(tmp_path: Path) -> list[str]:
    source = _extract("typescript", r"^function verifySkyvernSignature\b.*?^\}\n")
    # Node runs the snippet as plain JavaScript; drop the TypeScript-only annotations.
    # The substitution count is asserted so a changed signature fails loudly here.
    source, substitutions = re.subn(r"\(rawBody: Buffer, signature: unknown\): boolean", "(rawBody, signature)", source)
    assert substitutions == 1, "TypeScript verifier signature changed; update the annotation strip"

    driver = tmp_path / "driver.js"
    driver.write_text(
        'const crypto = require("crypto");\n\n'
        f"{source}\n"
        + textwrap.dedent(
            """
            const spec = JSON.parse(process.argv[2]);
            const rawBody = Buffer.from(spec.rawBody, "base64");
            const verdicts = {};
            for (const [label, signature] of spec.cases) {
              verdicts[label] = verifySkyvernSignature(rawBody, signature === null ? undefined : signature);
            }
            console.log(JSON.stringify(verdicts));
            """
        ),
        encoding="utf-8",
    )

    return ["node", str(driver), SPEC]


def _go_driver(tmp_path: Path) -> list[str]:
    source = _extract("go", r"^func verifySkyvernSignature\b.*?^\}\n")

    driver = tmp_path / "main.go"
    driver.write_text(
        textwrap.dedent(
            """\
            package main

            import (
                "crypto/hmac"
                "crypto/sha256"
                "encoding/hex"
                "encoding/base64"
                "encoding/json"
                "fmt"
                "os"
            )

            """
        )
        + f"{source}\n"
        + textwrap.dedent(
            """
            func main() {
                var spec struct {
                    RawBody string     `json:"rawBody"`
                    Cases   [][]*string `json:"cases"`
                }
                if err := json.Unmarshal([]byte(os.Args[1]), &spec); err != nil {
                    panic(err)
                }
                rawBody, err := base64.StdEncoding.DecodeString(spec.RawBody)
                if err != nil {
                    panic(err)
                }
                verdicts := map[string]bool{}
                for _, c := range spec.Cases {
                    signature := ""
                    if c[1] != nil {
                        signature = *c[1]
                    }
                    verdicts[*c[0]] = verifySkyvernSignature(rawBody, signature)
                }
                out, _ := json.Marshal(verdicts)
                fmt.Println(string(out))
            }
            """
        ),
        encoding="utf-8",
    )

    return ["go", "run", str(driver), SPEC]


RUNTIMES = [("node", _typescript_driver), ("go", _go_driver)]


@pytest.mark.parametrize("runtime,build_driver", RUNTIMES, ids=[r[0] for r in RUNTIMES])
def test_snippet_rejects_forged_and_missing_signatures(
    runtime: str, build_driver: Callable[[Path], list[str]], tmp_path: Path
) -> None:
    _require_runtime(runtime)
    verdicts = _run_driver(build_driver(tmp_path), tmp_path)
    assert verdicts is not None
    _assert_verdicts(verdicts)


@pytest.mark.parametrize("runtime,build_driver", RUNTIMES, ids=[r[0] for r in RUNTIMES])
def test_snippet_does_not_accept_when_api_key_is_absent(
    runtime: str, build_driver: Callable[[Path], list[str]], tmp_path: Path
) -> None:
    """An unset key must not sign with "" -- a key an attacker also knows."""
    _require_runtime(runtime)
    verdicts = _run_driver(build_driver(tmp_path), tmp_path, api_key=None)
    if verdicts is None:
        return  # refused to run without a key, which is also fail-closed
    assert verdicts["forged_with_empty_key"] is False, (
        f"the {runtime} verifier signs with the empty key when SKYVERN_API_KEY is unset, "
        "so anyone can forge a valid signature"
    )
    assert verdicts["genuine"] is False


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
