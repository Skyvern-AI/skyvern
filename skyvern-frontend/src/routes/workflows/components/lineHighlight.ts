import { type EditorState, type Range, StateField } from "@codemirror/state";
import { Decoration, type DecorationSet, EditorView } from "@codemirror/view";
import type { Extension } from "@uiw/react-codemirror";

export type LineHighlightRange = {
  from: number;
  to: number;
  variant?: "active" | "error";
};

const activeLineDecoration = Decoration.line({
  class: "cm-line-highlight-active",
});
const errorLineDecoration = Decoration.line({
  class: "cm-line-highlight-error",
});

const lineHighlightTheme = EditorView.baseTheme({
  ".cm-line-highlight-active": {
    backgroundColor: "rgba(125, 211, 252, 0.14)",
  },
  ".cm-line-highlight-error": {
    backgroundColor: "rgba(248, 113, 113, 0.16)",
  },
});

function buildDecorations(
  state: EditorState,
  ranges: Array<LineHighlightRange>,
): DecorationSet {
  const lineCount = state.doc.lines;
  const decorations: Array<Range<Decoration>> = [];
  for (const range of ranges) {
    const start = Math.max(1, Math.min(range.from, range.to));
    const end = Math.min(lineCount, Math.max(range.from, range.to));
    const decoration =
      range.variant === "error" ? errorLineDecoration : activeLineDecoration;
    for (let lineNumber = start; lineNumber <= end; lineNumber++) {
      decorations.push(decoration.range(state.doc.line(lineNumber).from));
    }
  }
  return Decoration.set(decorations, true);
}

export function lineHighlight(ranges: Array<LineHighlightRange>): Extension {
  if (ranges.length === 0) {
    return [];
  }
  const field = StateField.define<DecorationSet>({
    create(state) {
      return buildDecorations(state, ranges);
    },
    update(decorations, transaction) {
      return transaction.docChanged
        ? buildDecorations(transaction.state, ranges)
        : decorations;
    },
    provide: (self) => EditorView.decorations.from(self),
  });
  return [field, lineHighlightTheme];
}
