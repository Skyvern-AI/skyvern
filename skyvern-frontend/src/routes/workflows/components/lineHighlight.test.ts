// @vitest-environment jsdom

import { EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { afterEach, describe, expect, test } from "vitest";

import { lineHighlight } from "./lineHighlight";

let view: EditorView | null = null;

afterEach(() => {
  view?.destroy();
  view = null;
  document.body.innerHTML = "";
});

function renderDoc(doc: string, extension: ReturnType<typeof lineHighlight>) {
  view = new EditorView({
    state: EditorState.create({ doc, extensions: [extension] }),
    parent: document.body,
  });
  return view;
}

function highlightedTexts(rendered: EditorView, className: string) {
  return Array.from(
    rendered.contentDOM.querySelectorAll(`.${className}`),
    (el) => el.textContent,
  );
}

describe("lineHighlight", () => {
  test("paints the requested 1-based inclusive line range as active", () => {
    const rendered = renderDoc(
      "line one\nline two\nline three",
      lineHighlight([{ from: 2, to: 3 }]),
    );

    expect(highlightedTexts(rendered, "cm-line-highlight-active")).toEqual([
      "line two",
      "line three",
    ]);
  });

  test("uses the error variant class for error ranges", () => {
    const rendered = renderDoc(
      "alpha\nbeta\ngamma",
      lineHighlight([{ from: 2, to: 2, variant: "error" }]),
    );

    expect(highlightedTexts(rendered, "cm-line-highlight-error")).toEqual([
      "beta",
    ]);
  });

  test("clamps ranges that fall outside the document", () => {
    const rendered = renderDoc(
      "only line",
      lineHighlight([{ from: 5, to: 9 }]),
    );

    expect(highlightedTexts(rendered, "cm-line-highlight-active")).toEqual([]);
  });

  test("returns no extension for an empty range list", () => {
    expect(lineHighlight([])).toEqual([]);
  });
});
