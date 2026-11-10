export type DocStarterProgress = {
  steps: number;
  usedReference: boolean;
  nested: boolean;
  hasContainer: boolean;
  problems: number;
  ran: boolean;
};

export const TIPS: Array<{
  id: string;
  when: (p: DocStarterProgress) => boolean;
  text: string;
}> = [
  {
    id: "slash",
    when: () => true,
    text: "On any line, type / to add a step, or @ to use an input or an earlier result.",
  },
  {
    id: "step",
    when: (p) => p.steps > 0,
    text: "It's a step now. Backspace at the start of its line turns it back into a note.",
  },
  {
    id: "nest",
    when: (p) => p.hasContainer,
    text: "Tab moves a line into the loop or If above it. Shift+Tab moves it back out.",
  },
  {
    id: "problems",
    when: (p) => p.problems > 0,
    text: "What a step still needs is shown under it as you go. Run waits until it's fixed.",
  },
  {
    id: "run",
    when: (p) => p.steps >= 2 && p.problems === 0,
    text: "Ready to try it? Press Run, top right.",
  },
];

/** The tip the Doc would show now, or -1; the checklist waits while one is showing. */
export function docTipIndex(progress: DocStarterProgress, seen: string[]) {
  return TIPS.findIndex((t) => !seen.includes(t.id) && t.when(progress));
}
