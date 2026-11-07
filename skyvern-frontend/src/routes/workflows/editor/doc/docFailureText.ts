// What a step's failure says in plain words: the agent's retry-limit wording becomes "Gave up after N tries", and
// a page element id it quotes (four characters starting "AA", after "element" or before "itself") becomes "that element".
const RETRY_LIMIT =
  /Max retries per step \((\d+)\) exceeded\.?\s*(?:Possible failure reasons?:\s*)?/i;
const ELEMENT_ID =
  /(?<=\b(?:element|id)\s+)AA[A-Za-z0-9]{2}\b|\bAA[A-Za-z0-9]{2}(?= itself\b)/g;

export function plainFailureText(raw: string): string {
  const text = raw.replace(ELEMENT_ID, "that element").trim();
  const limit = text.match(RETRY_LIMIT);
  if (!limit) return text;
  const tries = `Gave up after ${limit[1]} tries`;
  // The line holds the first sentence of the reason; the expanded result has the rest.
  const rest = text.slice(limit.index! + limit[0].length).trim();
  const reason = rest.match(/^.+?[.!?](?=\s|$)/)?.[0] ?? rest;
  return reason ? `${tries}: ${reason}` : tries;
}

// The server's MissingStarterUrl text, which names the step's internal label and template syntax.
const NO_START_PAGE = /has no starting URL set/i;
export const NO_START_PAGE_TEXT =
  "This step needs a page to start on. Run from the Go to URL step, or run the whole agent.";

export function failureHeadlineInPlainWords(
  raw: string,
  headlineOf: (raw: string) => string,
): string {
  if (NO_START_PAGE.test(raw)) return NO_START_PAGE_TEXT;
  return RETRY_LIMIT.test(raw)
    ? plainFailureText(raw)
    : plainFailureText(headlineOf(raw));
}
