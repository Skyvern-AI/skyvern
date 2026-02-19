# Credential Management

## Naming convention

Use environment and target domain in credential names.

Example: `prod-salesforce-primary` or `staging-hubspot-sandbox`.

## Lifecycle

1. Create/store credential in vault.
2. Validate login once.
3. Reuse by ID in automation.
4. Rotate and retire on schedule.

## Safety checks

- Never print secrets in logs.
- Confirm credential IDs map to the expected system.
- Delete stale credentials proactively.
