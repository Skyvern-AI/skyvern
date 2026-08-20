# SKY-13117: Keep run-count axis units consistent

## Scope

- Update `skyvern-frontend/cloud/routes/analytics/components/RunStatusHeroChart.tsx`
  so every run-count tick on a rendered axis uses one shared magnitude suffix.
- Update
  `skyvern-frontend/cloud/routes/analytics/components/RunStatusHeroChart.test.tsx`
  with regression coverage for a thousands-scale axis ending at one million.

## Acceptance criteria

- A thousands-scale axis renders `250K`, `500K`, `750K`, and `1,000K`.
- A single axis never switches from `K` to `M` between ticks.
- Small run-count axes remain locale-formatted without a suffix.
- The count axis remains wide enough for the longest expected compact label.

## Verification

- Run the focused `RunStatusHeroChart` Vitest test.
- Run ESLint on the two changed frontend source files.
