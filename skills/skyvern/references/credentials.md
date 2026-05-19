# Credential Management

## Create credentials (non-interactive)

Use env vars for secrets — CLI flags are visible in `ps` and `/proc/*/cmdline`.

```bash
# Set secrets as env vars BEFORE the command (not inline — avoids shell history leak)
export SKYVERN_CRED_PASSWORD="s3cret"
skyvern credentials add --name "prod-login" --type password \
  --username "user@example.com" --json

# With TOTP
export SKYVERN_CRED_PASSWORD="s3cret"
export SKYVERN_CRED_TOTP="JBSWY3DPEHPK3PXP"
skyvern credentials add --name "prod-mfa" --type password \
  --username "user@example.com" --json

# Credit card (sensitive fields via env vars)
export SKYVERN_CRED_CARD_NUMBER="4111111111111111"
export SKYVERN_CRED_CVV="123"
skyvern credentials add --name "test-card" --type credit_card \
  --exp-month "12" --exp-year "2027" --card-brand "visa" \
  --holder-name "John Doe" --json

# Secret (API key, token, etc.)
export SKYVERN_CRED_SECRET_VALUE="sk_live_abc123"
skyvern credentials add --name "api-token" --type secret \
  --secret-label "Stripe key" --json
```

Set `SKYVERN_NON_INTERACTIVE=1` to ensure prompts never fire.

## List and lookup

```bash
skyvern credential list --json
skyvern credential get --id cred_abc123 --json
```

## Delete

```bash
skyvern credentials delete cred_abc123 --yes --json
```

## Naming convention

Use environment and target domain: `prod-salesforce-primary`, `staging-hubspot-sandbox`.

## Safety

- Use env vars for secrets, not CLI flags.
- Never print secrets in logs.
- Confirm credential IDs map to the expected system.
- Delete stale credentials proactively.
