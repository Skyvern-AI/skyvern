// The line one answered part contributes to the reply. Its own file so the card
// stays component-only (Fast Refresh), matching connectedAccountChoiceLabel.ts.
// Exported because the answered state a reloaded chat shows is read back off the
// sent message rather than from component state the reload threw away.
export function questionAnswerLine(prompt: string, choice: string): string {
  return `${prompt} — ${choice}`;
}
