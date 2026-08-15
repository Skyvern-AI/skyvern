export const COPILOT_WORKING_VERBS = [
  "Gliding through the DOM",
  "Nesting the selectors",
  "Perching above the fold",
  "Riding the event loop",
  "Migrating between tabs",
  "Skimming the scroll",
  "Threading the iframe",
  "Scanning the waterfall",
  "Quartering the viewport",
  "Guarding the cache",
  "Hoarding the cookies",
  "Riding the thermals",
  "Scouting the skies",
  "Unfurling wings",
  "Hatching your agent",
  "Kindling the fire",
  "Banking left",
  "Cresting the clouds",
  "Diving in",
  "Scanning the horizon",
];

export const VERB_CYCLE_MS = 3000;

export function pickWorkingVerb(previous?: string): string {
  const pool = COPILOT_WORKING_VERBS.filter((verb) => verb !== previous);
  return pool[Math.floor(Math.random() * pool.length)]!;
}
