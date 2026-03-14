# Complex Input Handling

## Date pickers

- Prefer intent: "set start date to 2026-03-15".
- If widget blocks typing, click field then choose date from calendar controls.

## File uploads

- Ensure file path exists before automation.
- Confirm uploaded filename appears in UI before submit.

## Dependent dropdowns

- Select parent option first.
- Wait for child options to refresh.
- Validate chosen value is still selected before moving on.

## Rich text editors

- Use focused intent like "enter summary text in the message editor".
- Validate rendered value, not only keystroke success.
