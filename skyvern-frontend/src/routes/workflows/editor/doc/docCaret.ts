// The line's text on each side of the caret, with line breaks; null when the caret is elsewhere.
export function splitAtCaret(el: HTMLElement): [string, string] | null {
  const selection = window.getSelection();
  if (!selection?.rangeCount) return null;
  const caret = selection.getRangeAt(0);
  if (!el.contains(caret.startContainer)) return null;
  const read = (range: Range) => {
    let out = "";
    const walk = (node: Node) => {
      if (node.nodeType === Node.TEXT_NODE) out += node.nodeValue ?? "";
      else if (node.nodeName === "BR") out += "\n";
      else node.childNodes.forEach(walk);
    };
    walk(range.cloneContents());
    return out;
  };
  const before = document.createRange();
  before.selectNodeContents(el);
  before.setEnd(caret.startContainer, caret.startOffset);
  const after = document.createRange();
  after.selectNodeContents(el);
  after.setStart(caret.endContainer, caret.endOffset);
  return [read(before), read(after)];
}

// The box of the character next to the caret, found by offset, for a caret that has no box of its own.
function charRectNextToCaret(el: HTMLElement, caret: Range): DOMRect | null {
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  const probe = document.createRange();
  const at = caret.startContainer;
  if (at.nodeType === Node.TEXT_NODE && (at as Text).length) {
    const length = (at as Text).length;
    const from = Math.min(caret.startOffset, length - 1);
    probe.setStart(at, from);
    probe.setEnd(at, from + 1);
    return probe.getClientRects()[0] ?? null;
  }
  let previous: Text | null = null;
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node as Text;
    if (!text.length) continue;
    if (caret.comparePoint(text, 0) >= 0) {
      probe.setStart(text, 0);
      probe.setEnd(text, 1);
      return probe.getClientRects()[0] ?? null;
    }
    previous = text;
  }
  if (!previous) return null;
  probe.setStart(previous, previous.length - 1);
  probe.setEnd(previous, previous.length);
  return probe.getClientRects()[0] ?? null;
}

// Whether the caret sits on the first or last visual line of the text, where an arrow key leaves it.
export function caretOnEdge(el: HTMLElement, edge: "first" | "last"): boolean {
  const selection = window.getSelection();
  if (!selection?.rangeCount || !selection.isCollapsed) return false;
  const sides = splitAtCaret(el);
  if (!sides) return false;
  if (sides[edge === "first" ? 0 : 1].includes("\n")) return false;
  const box = el.getBoundingClientRect();
  const range = selection.getRangeAt(0).cloneRange();
  let rect = range.getClientRects()[0] ?? range.getBoundingClientRect();
  // A caret set on the element itself (not in a text node) has a zero-height rect. Measure the character next to it.
  if (box.height && !rect.height) {
    const nearby = charRectNextToCaret(el, range);
    if (nearby) rect = nearby;
  }
  // Not laid out, or no character to measure (an empty line): the text on that side decided it.
  if (!box.height || !rect.height) return true;
  return edge === "first"
    ? rect.top - box.top < rect.height * 0.75
    : box.bottom - rect.bottom < rect.height * 0.75;
}
