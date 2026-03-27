# Agent Mode

Patterns for using the Skyvern CLI from AI agents and CI/CD pipelines.

## Discovery

```bash
# Top-level commands (~1.4K tokens, default)
skyvern capabilities --json

# Drill into a specific command group
skyvern capabilities workflow --json

# Full tree (~20K tokens, opt-in)
skyvern capabilities --depth 3 --json
```

## Non-interactive mode

Set `SKYVERN_NON_INTERACTIVE=1` or `CI=true` to prevent interactive prompts.
All required values must be passed via env vars or flags. Errors return JSON when `--json` is set.

## Credentials (secrets)

Use env vars for secrets — CLI flags are visible in `ps` and `/proc/*/cmdline`.

```bash
# Set secrets as env vars BEFORE the command (not inline — avoids shell history leak)
export SKYVERN_CRED_PASSWORD="s3cret"
export SKYVERN_CRED_TOTP="JBSWY3DP"
skyvern credentials add --name "prod-login" --type password \
  --username "user@example.com" --json

# Credit card
export SKYVERN_CRED_CARD_NUMBER="4111111111111111"
export SKYVERN_CRED_CVV="123"
skyvern credentials add --name "test-card" --type credit_card \
  --exp-month 12 --exp-year 2027 --card-brand visa --holder-name "John Doe" --json

# Secret
export SKYVERN_CRED_SECRET_VALUE="sk_live_abc123"
skyvern credentials add --name "api-token" --type secret --json

# Delete without confirmation
skyvern credentials delete cred_abc123 --yes --json
```

Available env vars: `SKYVERN_CRED_PASSWORD`, `SKYVERN_CRED_TOTP`,
`SKYVERN_CRED_CARD_NUMBER`, `SKYVERN_CRED_CVV`, `SKYVERN_CRED_SECRET_VALUE`,
`SKYVERN_CRED_USERNAME`, `SKYVERN_CRED_EXP_MONTH`, `SKYVERN_CRED_EXP_YEAR`,
`SKYVERN_CRED_CARD_BRAND`, `SKYVERN_CRED_HOLDER_NAME`, `SKYVERN_CRED_SECRET_LABEL`.

## Other commands

```bash
skyvern run ui --force
skyvern workflow list --json | jq '.ok'
skyvern status --json | jq '.data'
```

## Structured output

Every `--json` response uses the same envelope:
`{schema_version, ok, action, data, error, warnings, browser_context, artifacts, timing_ms}`
