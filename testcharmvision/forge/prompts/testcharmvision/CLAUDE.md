# Prompt Templates

Action extraction templates are split for caching optimization:

- `extract-action.j2` — Complete template (static + dynamic)
- `extract-action-static.j2` — Cacheable prefix
- `extract-action-dynamic.j2` — Dynamic suffix with runtime variables

When modifying `extract-action.j2`, update the static/dynamic files to match. The static file must exactly match the prefix of the complete file.
