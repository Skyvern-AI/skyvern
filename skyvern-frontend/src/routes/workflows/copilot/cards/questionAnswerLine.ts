// The wire format the card answers in. This is a PERSISTED contract, not an internal detail: a
// card rendered today reads the answered state back out of a message written weeks ago, so
// changing any of it silently orphans the answered state of every chat already stored.
// `questionAnswerLine.test.ts` pins these as LITERAL strings for that reason — assert the text,
// never a second call to these functions.
const ANSWER_SEPARATOR = " — ";

// The prefix the card emits before an answer. Matching on this, rather than on the user's words,
// is what keeps the reload receipt reading the card's own output.
export function questionAnswerLabel(prompt: string): string {
  return `${prompt}${ANSWER_SEPARATOR}`;
}

export function questionAnswerLine(prompt: string, answer: string): string {
  return `${questionAnswerLabel(prompt)}${answer}`;
}

// Two parts may carry the same prompt, and the emitted line is all a reloaded card has to go on:
// one line reading "Which day? — Friday" cannot say which twin the user answered. So a repeat
// occurrence takes an ordinal and matches its own label.
export function twinPrompt(prompt: string, ordinal: number): string {
  return `${prompt} (${ordinal})`;
}

// Uniqueness has to hold in the space the line is actually WRITTEN in. The emitted line is
// whitespace-normalized, and that is lossy: "Which\nstore?" and "Which store?" are different
// prompts that would persist the same prefix, so answering the second would reload onto the
// first. Labels are therefore reserved by their normalized form, not their raw one.
const normalizeLabel = (value: string) => value.replace(/\s+/g, " ").trim();

// The prompt each part emits and matches on, in card order. The FIRST occurrence keeps the plain
// prompt — so a message written before ordinals existed still resolves to it — and only later
// collisions are suffixed.
export function emissionPrompts(prompts: string[]): string[] {
  const taken = new Set<string>();
  return prompts.map((prompt) => {
    let label = prompt;
    let ordinal = 1;
    while (taken.has(normalizeLabel(label))) {
      ordinal += 1;
      label = twinPrompt(prompt, ordinal);
    }
    taken.add(normalizeLabel(label));
    return label;
  });
}
