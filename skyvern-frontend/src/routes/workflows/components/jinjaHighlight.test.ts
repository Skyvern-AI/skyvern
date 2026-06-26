// @vitest-environment jsdom

import { EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { afterEach, describe, expect, test } from "vitest";

import { jinjaHighlight } from "./jinjaHighlight";

let view: EditorView | null = null;

afterEach(() => {
  view?.destroy();
  view = null;
  document.body.innerHTML = "";
});

function renderDoc(doc: string) {
  view = new EditorView({
    state: EditorState.create({ doc, extensions: [jinjaHighlight] }),
    parent: document.body,
  });
  return view;
}

function markedTexts(renderedView: EditorView) {
  return Array.from(
    renderedView.contentDOM.querySelectorAll(".cm-jinja-param"),
    (el) => el.textContent,
  );
}

describe("jinjaHighlight", () => {
  test("marks jinja parameter expressions, including dotted paths", () => {
    const rendered = renderDoc("Open {{ url }} as {{ user.name }}");

    expect(markedTexts(rendered)).toEqual(["{{ url }}", "{{ user.name }}"]);
  });

  test("marks parameters written without inner spacing", () => {
    const rendered = renderDoc("Go to {{url}}");

    expect(markedTexts(rendered)).toEqual(["{{url}}"]);
  });

  test("leaves plain text, single braces, and unclosed braces unmarked", () => {
    const rendered = renderDoc("Open { url } and {{ unclosed");

    expect(markedTexts(rendered)).toEqual([]);
  });

  test("keeps marks in sync as the document changes", () => {
    const rendered = renderDoc("no params yet");

    rendered.dispatch({
      changes: {
        from: 0,
        to: rendered.state.doc.length,
        insert: "now {{ filled }}",
      },
    });

    expect(markedTexts(rendered)).toEqual(["{{ filled }}"]);
  });
});
