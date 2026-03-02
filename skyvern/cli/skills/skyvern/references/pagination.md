# Pagination Strategy

## Stable sequence

1. Extract data on current page.
2. Validate non-empty result.
3. Advance using intent ("Next page"), not hardcoded selectors.
4. Stop on explicit condition:
- no next page,
- duplicate first row,
- max page limit reached.

## Guardrails

- Record page index in output metadata.
- Deduplicate by a stable key (`id`, `url`, `title+date`).
- Fail fast if extraction shape changes unexpectedly.
