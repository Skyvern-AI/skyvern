# Screenshot-led Debugging

## Capture points

- before the failing action
- immediately after the failing action
- after wait/validation conditions

## What to inspect

- visibility of target controls
- modal overlays blocking interaction
- error banners or toast messages
- unexpected route changes

## Fast loop

1. Capture screenshot.
2. Adjust one variable (prompt, wait, selector).
3. Rerun and compare screenshot delta.
