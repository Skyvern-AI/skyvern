"""Executes the published TypeScript and Go webhook-signature verifiers under Node and Go.

They live apart from the Python snippet test so CI can run them only when the snippet or its signer changes.
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

from tests.unit.test_docs_webhook_verification_snippets import API_KEY, CASES, RAW_BODY, _assert_verdicts, _extract

SPEC = json.dumps({"rawBody": base64.b64encode(RAW_BODY).decode(), "cases": [[c[0], c[1]] for c in CASES]})


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
