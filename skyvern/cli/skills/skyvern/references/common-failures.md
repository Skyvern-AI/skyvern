# Common Failure Patterns

## Symptom: action clicked wrong element

Likely cause: ambiguous intent or crowded UI.

Fix:
- add stronger context in prompt (position, label, section)
- fall back to hybrid selector + intent when necessary

## Symptom: extraction returns empty arrays

Likely cause: content not loaded or schema too strict.

Fix:
- wait for content-ready condition
- temporarily relax required fields
- validate visible row/card count before extract

## Symptom: login passes but next step fails as logged out

Likely cause: session mismatch or redirect race.

Fix:
- ensure same `session_id` across steps
- add post-login `validate` check before continuing
