// Isolate controls inside the text would close or extend the wrapping isolate early.
export function stripBidiIsolates(text: string) {
  return text.replace(/[⁦-⁩]/g, "");
}

// First-strong isolate, so a right-to-left run in a typed name cannot reorder the text around it.
export function bidiIsolate(text: string) {
  return `⁨${stripBidiIsolates(text)}⁩`;
}
