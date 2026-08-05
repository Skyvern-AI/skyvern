# SKY-13291 vendor live-view metadata persistence

## Scope

This PR is the persistence prerequisite for vendor live view. It captures the credential-bearing
live-view URL and a provider-neutral protocol value at the provider response, carries them only in
the vendor provisioning activity, and stores them only in `browser_session_infra`.

It does not add a viewer, router relay, API field, CSP rule, or frontend code. Those paths remain
blocked on a documented raw RFB endpoint for headful Anchor sessions and a Browser Use strategy
that does not send vendor HTML or JavaScript to customers.

## Implementation

1. Add nullable `live_view_url` and `live_view_protocol` columns to
   `browser_session_infra` with one additive Alembic migration.
2. Extend the cloud-only infrastructure repository to atomically attach the provider session id
   and live-view metadata after provisioning. Keep both sensitive fields out of dataclass reprs.
3. Capture Anchor's live-view URL from both supported provisioning transports. Classify current
   headful sessions as `rfb` and future explicitly headless sessions as `cdp_screencast`.
4. Capture Browser Use's `live_url` and classify it as `hosted_html`.
5. Carry the metadata in the activity-local `VendorBrowser` only. Do not add it to
   `VendorProvisionResult`, `PersistentBrowserSession`, or any API schema.

## Acceptance criteria

- A successful provider provision stores its live-view URL and generic protocol in
  `browser_session_infra` together with the provider session id.
- Missing live-view metadata remains nullable and does not make an otherwise usable browser fail.
- The credential does not appear in object repr/string output, structured logs, Temporal activity
  payloads, persistent browser-session models, OpenAPI schemas, or client code.
- Existing provider-identity non-disclosure tests remain green.
- The migration graph has exactly one head.

## Verification

```bash
uv run pytest tests/cloud/test_anchor_session_provisioner.py \
  tests/cloud/test_browser_session_infra.py \
  tests/cloud/test_pbs_vendor_session_lifecycle.py \
  tests/cloud/test_vendor_identity_nondisclosure.py \
  tests/unit/webeye/test_browser_session_response.py -q
uv run pytest tests/cloud/test_migration_chain.py -q
uv run alembic heads
uv run python -m py_compile <each modified Python file>
uv run ruff check <modified Python files>
uv run pre-commit run --files <modified files>
```

Each security assertion is mutation-checked by temporarily removing its production guard, running
the focused test to observe failure, restoring the guard, and rerunning the test to green.

## Escalation: Size Limits

**What I tried**: Implemented the human-approved persistence-only slice with the six requested
mutation-proven confinement checks, then fixed three credential-safety findings from independent
review: SQL bound-parameter redaction, provider traceback-local scrubbing, and cancellation-safe
credential cleanup.

**What blocked me**: The resulting code diff is 617 changed lines excluding this plan and the new
migration: 584 additions and 33 deletions across five production and four test files. The
repository stop limit is 500 changed lines. The split is 197 production lines and 420 test lines;
the requested security and mutation coverage accounts for most of the excess. The change remains
within the file-count and subsystem limits.

**Decision needed**: A human must either approve a size-limit exception for this security-focused
slice or direct a specific reduction/split. No commit, push, or PR will be created without that
decision.

**My recommendation**: Approve the exception for this slice. Splitting the confinement tests from
the credential persistence they protect would make either PR independently unsafe, and deleting
tests would weaken the explicit mutation-proof requirement.
