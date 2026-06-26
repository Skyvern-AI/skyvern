import {
  Decoration,
  type DecorationSet,
  EditorView,
  MatchDecorator,
  ViewPlugin,
  type ViewUpdate,
} from "@codemirror/view";

const jinjaMatcher = new MatchDecorator({
  regexp: /\{\{\s*[\w.]+\s*\}\}/g,
  decoration: Decoration.mark({ class: "cm-jinja-param" }),
});

const jinjaTheme = EditorView.baseTheme({
  ".cm-jinja-param": {
    color: "#7dd3fc",
    backgroundColor: "rgba(125, 211, 252, 0.12)",
    borderRadius: "3px",
    fontWeight: "600",
  },
});

const jinjaPlugin = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet;
    constructor(view: EditorView) {
      this.decorations = jinjaMatcher.createDeco(view);
    }
    update(update: ViewUpdate) {
      this.decorations = jinjaMatcher.updateDeco(update, this.decorations);
    }
  },
  { decorations: (v) => v.decorations },
);

export const jinjaHighlight = [jinjaPlugin, jinjaTheme];
