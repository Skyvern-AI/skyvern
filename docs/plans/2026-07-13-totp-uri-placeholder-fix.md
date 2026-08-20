# TOTP issuer mismatch and placeholder fail-closed plan

## Scope

Fix standard `otpauth://` TOTP URIs whose label issuer differs from the `issuer` query parameter, while retaining strict validation of the secret and runtime configuration. Prevent unresolved TOTP placeholders from reaching browser input paths.

## Implementation

1. Add regression tests in `tests/unit/test_parse_totp_secret.py` for raw and percent-encoded issuer mismatches, non-default TOTP settings, and malformed configurations that must remain rejected.
2. Update `skyvern/forge/sdk/services/credentials.py` so strict `pyotp.parse_uri()` validation remains the default and only the issuer-mismatch error gets a validation-only retry without issuer query metadata.
3. Add handler and generated-script tests proving missing TOTP material raises a typed failure and browser typing is never called across DOM, selectable, CUA, and autocomplete paths.
4. Update `skyvern/webeye/actions/handler.py` and `skyvern/core/script_generations/skyvern_page.py` so every input path resolves a TOTP marker before branching or typing.

## Acceptance criteria

- Mismatched label/query issuers generate the correct code while preserving algorithm, digit count, and period.
- Matching-issuer URIs and raw Base32 secrets behave unchanged.
- Missing or invalid secrets, unsupported algorithms or digit counts, malformed periods, unknown parameters, and HOTP URIs remain rejected.
- No unresolved `placeholder_*_totp` value is passed to a browser typing method.
- Failures do not expose the URI, secret, placeholder, or generated code.

## Verification

- Run targeted parser and handler tests during TDD.
- Compile and lint every modified Python file.
- Run the broader credential/TOTP regression suites and all unit tests.
- Run pre-commit on the changed files and `git diff --check`.
- Obtain independent parser/security and action-failure reviews, then complete a separate QA pass.
