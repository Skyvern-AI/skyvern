// @vitest-environment jsdom

import { python } from "@codemirror/lang-python";
import { diagnosticCount, forceLinting } from "@codemirror/lint";
import { EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { waitFor } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import {
  getPythonSyntaxDiagnostics,
  pythonSyntaxExtensions,
} from "./pythonSyntaxLinter";

describe("getPythonSyntaxDiagnostics", () => {
  test("returns no diagnostics for an empty code block", () => {
    expect(getPythonSyntaxDiagnostics("")).toEqual([]);
  });

  test("returns no diagnostics for valid code-block Python", () => {
    expect(
      getPythonSyntaxDiagnostics(
        'title = await page.title()\nreturn {"title": title}',
      ),
    ).toEqual([]);
  });

  test("does not flag code-block Jinja parameters", () => {
    expect(
      getPythonSyntaxDiagnostics(
        'await page.goto("{{ url }}")\nreturn {"value": {{ value }}}',
      ),
    ).toEqual([]);
  });

  test("does not flag Jinja-only expression syntax", () => {
    expect(
      getPythonSyntaxDiagnostics(
        "first = {{ values.0 }}\nsecond = {{ 1 ~ 2 }}",
      ),
    ).toEqual([]);
  });

  test("does not flag code-block Jinja control statements or comments", () => {
    expect(
      getPythonSyntaxDiagnostics(
        "{% if enabled %}\n    x = 1\n{# keep this assignment conditional #}\n{% endif %}",
      ),
    ).toEqual([]);
  });

  test("does not parse mutually exclusive Jinja branches together", () => {
    expect(
      getPythonSyntaxDiagnostics(
        "value = {% if enabled %}1{% else %}2{% endif %}",
      ),
    ).toEqual([]);
  });

  test("does not introduce indentation for inline Jinja control tags", () => {
    expect(
      getPythonSyntaxDiagnostics("{% if enabled %}x = 1{% endif %}"),
    ).toEqual([]);
  });

  test("still reports syntax errors from a Jinja branch", () => {
    const source = "value = {% if enabled %}({% else %}2{% endif %}";

    expect(getPythonSyntaxDiagnostics(source)).toEqual([
      expect.objectContaining({
        from: source.indexOf("("),
        to: source.indexOf("(") + 1,
        severity: "error",
      }),
    ]);
  });

  test("reports syntax errors from an alternate Jinja branch", () => {
    const source = "value = {% if enabled %}1{% else %}({% endif %}";

    expect(getPythonSyntaxDiagnostics(source)).toEqual([
      expect.objectContaining({
        from: source.indexOf("("),
        to: source.indexOf("(") + 1,
        severity: "error",
      }),
    ]);
  });

  test("does not preserve non-emitting newlines inside Jinja tags", () => {
    expect(
      getPythonSyntaxDiagnostics(
        "value = {% if\n enabled %}1{% else %}2{% endif %}",
      ),
    ).toEqual([]);
  });

  test("honors Jinja whitespace-control markers", () => {
    expect(
      getPythonSyntaxDiagnostics(
        "value = {% if enabled -%}\n1\n{%- else -%}\n2\n{%- endif %}",
      ),
    ).toEqual([]);
  });

  test("does not parse non-emitting Jinja macro bodies as Python", () => {
    expect(
      getPythonSyntaxDiagnostics(
        '{% macro greeting() %}hello world{% endmacro %}\nmessage = "{{ greeting() }}"',
      ),
    ).toEqual([]);
  });

  test("ignores Jinja control tags inside Python comments", () => {
    const source = "# {% macro greeting() %}\nvalue = 1 2\n# {% endmacro %}";

    expect(getPythonSyntaxDiagnostics(source)).toEqual([
      expect.objectContaining({
        from: source.indexOf("2"),
        to: source.indexOf("2") + 1,
        severity: "error",
      }),
    ]);
  });

  test("keeps diagnostics when Jinja branch variants exceed the cap", () => {
    const branches = Array.from(
      { length: 6 },
      (_, index) => `x${index} = {% if flag${index} %}1{% else %}2{% endif %}`,
    ).join("\n");
    const source = `${branches}\nvalue = 1 2`;

    expect(getPythonSyntaxDiagnostics(source)).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          from: source.lastIndexOf("2"),
          to: source.lastIndexOf("2") + 1,
          severity: "error",
        }),
      ]),
    );
  });

  test("keeps earlier alternate branches represented at the variant cap", () => {
    const firstBranch = "value = {% if flag0 %}1{% else %}({% endif %}";
    const followingBranches = Array.from(
      { length: 5 },
      (_, index) =>
        `x${index} = {% if flag${index + 1} %}1{% else %}2{% endif %}`,
    ).join("\n");
    const source = `${firstBranch}\n${followingBranches}`;

    expect(getPythonSyntaxDiagnostics(source)).not.toEqual([]);
  });

  test("parses Jinja raw-block contents as emitted Python", () => {
    const source = "{% raw %}\nvalue = {{ values.0 }}\n{% endraw %}";

    expect(getPythonSyntaxDiagnostics(source)).not.toEqual([]);
  });

  test("checks repeated Jinja loop body emissions", () => {
    expect(
      getPythonSyntaxDiagnostics(
        "value = {% for item in items %}1 {% else %}2{% endfor %}",
      ),
    ).not.toEqual([]);
  });

  test("preserves diagnostic offsets after multiline Jinja control statements", () => {
    const source = "{% if\n enabled %}\nx = 1\ny = (";

    expect(getPythonSyntaxDiagnostics(source)).toEqual([
      expect.objectContaining({
        from: source.indexOf("("),
        to: source.indexOf("(") + 1,
        severity: "error",
      }),
    ]);
  });

  test("marks invalid Python syntax with an error diagnostic", () => {
    expect(getPythonSyntaxDiagnostics("if True\n    return {}")).toEqual([
      expect.objectContaining({
        from: 3,
        to: 7,
        severity: "error",
        message: "Invalid Python syntax.",
      }),
    ]);
  });

  test("expands an end-of-file parser error to a visible underline", () => {
    expect(getPythonSyntaxDiagnostics("print(")).toEqual([
      expect.objectContaining({
        from: 5,
        to: 6,
        severity: "error",
      }),
    ]);
  });

  test("renders an underline and gutter marker in CodeMirror", async () => {
    const parent = document.createElement("div");
    document.body.append(parent);
    const view = new EditorView({
      parent,
      state: EditorState.create({
        doc: "if True\n    return {}",
        extensions: [python(), ...pythonSyntaxExtensions],
      }),
    });

    forceLinting(view);

    await waitFor(() => expect(diagnosticCount(view.state)).toBe(1));
    expect(parent.querySelector(".cm-lintRange-error")).not.toBeNull();
    expect(parent.querySelector(".cm-lint-marker-error")).not.toBeNull();

    view.destroy();
    parent.remove();
  });
});
