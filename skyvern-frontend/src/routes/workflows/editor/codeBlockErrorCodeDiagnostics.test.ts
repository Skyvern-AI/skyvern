import { describe, expect, test } from "vitest";

import { analyzeCodeBlockErrorCodes } from "./codeBlockErrorCodeDiagnostics";

describe("analyzeCodeBlockErrorCodes", () => {
  test("classifies declared, unused, and undeclared direct literal raises", () => {
    const result = analyzeCodeBlockErrorCodes(
      [
        'raise ErrorCode("matched", "reason")',
        'raise ErrorCode("raised_only", make_reason())',
        'raise ErrorCode("matched", dynamic_reason)',
      ].join("\n"),
      { matched: "when matched", declared_only: "when declared only" },
    );

    expect(result.declaredAndRaised).toEqual([
      { code: "matched", lines: [1, 3] },
    ]);
    expect(result.declaredButUnused).toEqual(["declared_only"]);
    expect(result.raisedButUndeclared).toEqual([
      { code: "raised_only", lines: [2] },
    ]);
    expect(result.malformedLines).toEqual([]);
  });

  test("matches backend literal parity for multiline, escaped, and adjacent strings", () => {
    const result = analyzeCodeBlockErrorCodes(
      [
        "raise ErrorCode(",
        "  'multi\\x2dline',",
        "  'reason',",
        ")",
        "raise ErrorCode('adjacent' '_code', 'reason')",
      ].join("\n"),
      { "multi-line": "multiline", adjacent_code: "adjacent" },
    );

    expect(result.declaredAndRaised).toEqual([
      { code: "multi-line", lines: [1] },
      { code: "adjacent_code", lines: [5] },
    ]);
    expect(result.malformedLines).toEqual([]);
  });

  test("removes Python line continuations from direct string literals", () => {
    const result = analyzeCodeBlockErrorCodes(
      [
        'raise ErrorCode("OUT_\\',
        'OF_STOCK", "why")',
        'raise ErrorCode("OUT_" "OF_STOCK", "why")',
        'raise ErrorCode("OUT_\\',
        'OF_STORE", "why")',
      ].join("\n"),
      { OUT_OF_STOCK: "declared" },
    );

    expect(result.declaredAndRaised).toEqual([
      { code: "OUT_OF_STOCK", lines: [1, 3] },
    ]);
    expect(result.declaredButUnused).toEqual([]);
    expect(result.raisedButUndeclared).toEqual([
      { code: "OUT_OF_STORE", lines: [4] },
    ]);
    expect(result.malformedLines).toEqual([]);
  });

  test("reports aliases, indirect, dynamic, keyword, and non-string calls as advisory malformed raises", () => {
    const result = analyzeCodeBlockErrorCodes(
      [
        "Alias = ErrorCode",
        "raise Alias('aliased', 'reason')",
        "raise errors.ErrorCode('indirect', 'reason')",
        "raise ErrorCode(dynamic_code, 'reason')",
        "raise ErrorCode(code='keyword', reasoning='reason')",
        "raise ErrorCode(123, 'reason')",
      ].join("\n"),
      null,
    );

    expect(result.malformedLines).toEqual([1, 2, 3, 4, 5, 6]);
    expect(result.declaredAndRaised).toEqual([]);
    expect(result.raisedButUndeclared).toEqual([]);
  });

  test.each([
    ["mixed positional and keyword", "raise ErrorCode('c', reasoning='r')"],
    [
      "multiline mixed positional and keyword",
      "raise ErrorCode(\n  'c',\n  reasoning='r',\n)",
    ],
    ["both keyword", "raise ErrorCode(error_code='c', reasoning='r')"],
    ["extra positional", "raise ErrorCode('c', 'r', 'extra')"],
    ["starred positional", "raise ErrorCode(*args)"],
    ["starred keyword", "raise ErrorCode(**kwargs)"],
  ])("rejects %s ErrorCode arguments", (_description, source) => {
    const result = analyzeCodeBlockErrorCodes(source, { c: "declared" });

    expect(result.declaredAndRaised).toEqual([]);
    expect(result.declaredButUnused).toEqual(["c"]);
    expect(result.raisedButUndeclared).toEqual([]);
    expect(result.malformedLines).toEqual([1]);
  });

  test("recognizes exactly two positional ErrorCode arguments", () => {
    const result = analyzeCodeBlockErrorCodes("raise ErrorCode('c', 'r')", {
      c: "declared",
    });

    expect(result.declaredAndRaised).toEqual([{ code: "c", lines: [1] }]);
    expect(result.malformedLines).toEqual([]);
  });

  test.each([
    ["positional walrus", "raise ErrorCode('c', reason := 'r')"],
    ["nested-call keyword", "raise ErrorCode('c', fmt('r', x=1))"],
  ])("accepts %s arguments", (_description, source) => {
    const result = analyzeCodeBlockErrorCodes(source, { c: "declared" });

    expect(result.declaredAndRaised).toEqual([{ code: "c", lines: [1] }]);
    expect(result.declaredButUnused).toEqual([]);
    expect(result.raisedButUndeclared).toEqual([]);
    expect(result.malformedLines).toEqual([]);
  });

  test.each([
    ["from package import ErrorCode", "raise ErrorCode('direct', 'reason')"],
    [
      "import package as ErrorCode",
      "raise ErrorCode('module_alias', 'reason')",
    ],
    ["from package import X as ErrorCode", "raise ErrorCode('x', 'reason')"],
  ])(
    "matches backend rejection when an import binds ErrorCode: %s",
    (importStatement, raiseStatement) => {
      const result = analyzeCodeBlockErrorCodes(
        [importStatement, raiseStatement].join("\n"),
        null,
      );

      expect(result.malformedLines).toEqual([1, 2]);
      expect(result.raisedButUndeclared).toEqual([]);
    },
  );

  test("does not treat an imported ErrorCode under another local name as an ErrorCode alias", () => {
    const result = analyzeCodeBlockErrorCodes(
      ["from package import ErrorCode as EC", 'raise EC("A", "reason")'].join(
        "\n",
      ),
      { A: "declared" },
    );

    expect(result.declaredAndRaised).toEqual([]);
    expect(result.declaredButUnused).toEqual(["A"]);
    expect(result.raisedButUndeclared).toEqual([]);
    expect(result.malformedLines).toEqual([]);
  });

  test.each([
    ["parenthesized from-import", "from package import (\n    ErrorCode,\n)"],
    ["backslash continuation", "from package import \\\n    ErrorCode"],
    [
      "parenthesized list",
      "from package import (\n    SomethingElse,\n    ErrorCode,\n    AnotherThing,\n)",
    ],
  ])("rejects %s bindings of ErrorCode", (_description, importStatement) => {
    const result = analyzeCodeBlockErrorCodes(
      `${importStatement}\nraise ErrorCode('direct', 'reason')`,
      null,
    );

    expect(result.malformedLines).toEqual([
      importStatement
        .split("\n")
        .findIndex((line) => line.includes("ErrorCode")) + 1,
      importStatement.split("\n").length + 1,
    ]);
    expect(result.raisedButUndeclared).toEqual([]);
  });

  test("does not treat a similarly named parenthesized import as ErrorCode", () => {
    const result = analyzeCodeBlockErrorCodes(
      "from package import (\n    ErrorCodeFoo,\n)\nraise ErrorCode('direct', 'reason')",
      null,
    );

    expect(result.malformedLines).toEqual([]);
    expect(result.raisedButUndeclared).toEqual([
      { code: "direct", lines: [4] },
    ]);
  });

  test.each([
    ["parenthesis in a string", 'text = "("'],
    ["parenthesis in a comment", "# ("],
    ["backslash at the end of a comment", "# \\"],
    ["braces and parentheses in an f-string", 'text = f"{value} ("'],
  ])("detects an import after a %s", (_description, precedingStatement) => {
    const result = analyzeCodeBlockErrorCodes(
      [
        precedingStatement,
        "from package import ErrorCode",
        'raise ErrorCode("DECLARED", "reason")',
      ].join("\n"),
      { DECLARED: "declared" },
    );

    expect(result.declaredAndRaised).toEqual([]);
    expect(result.declaredButUnused).toEqual(["DECLARED"]);
    expect(result.malformedLines).toEqual([2, 3]);
  });

  test("detects an import after a multiline string line ending in a backslash", () => {
    const result = analyzeCodeBlockErrorCodes(
      [
        'text = """value\\',
        '"""',
        "from package import ErrorCode",
        'raise ErrorCode("DECLARED", "reason")',
      ].join("\n"),
      { DECLARED: "declared" },
    );

    expect(result.declaredAndRaised).toEqual([]);
    expect(result.declaredButUnused).toEqual(["DECLARED"]);
    expect(result.malformedLines).toEqual([3, 4]);
  });

  test("ignores import text in a triple-quoted string and detects a subsequent real import", () => {
    const result = analyzeCodeBlockErrorCodes(
      [
        'text = """',
        "from fake import ErrorCode",
        '"""',
        "from package import ErrorCode",
        'raise ErrorCode("DECLARED", "reason")',
      ].join("\n"),
      { DECLARED: "declared" },
    );

    expect(result.declaredAndRaised).toEqual([]);
    expect(result.declaredButUnused).toEqual(["DECLARED"]);
    expect(result.malformedLines).toEqual([4, 5]);
  });

  test("does not treat import text in a triple-quoted string as a binding", () => {
    const result = analyzeCodeBlockErrorCodes(
      [
        'text = """',
        "from fake import ErrorCode",
        '"""',
        'raise ErrorCode("DECLARED", "reason")',
      ].join("\n"),
      { DECLARED: "declared" },
    );

    expect(result.declaredAndRaised).toEqual([
      { code: "DECLARED", lines: [4] },
    ]);
    expect(result.malformedLines).toEqual([]);
  });

  test("returns no manifest mismatch when the effective manifest is null", () => {
    const result = analyzeCodeBlockErrorCodes("print('ok')", null);
    expect(result).toEqual({
      declaredAndRaised: [],
      declaredButUnused: [],
      raisedButUndeclared: [],
      malformedLines: [],
    });
  });

  test("treats a shadowed ErrorCode constructor as malformed", () => {
    const result = analyzeCodeBlockErrorCodes(
      "ErrorCode = custom_error\nraise ErrorCode('shadowed', 'reason')",
      { shadowed: "must not match the runtime primitive" },
    );
    expect(result.declaredAndRaised).toEqual([]);
    expect(result.declaredButUnused).toEqual(["shadowed"]);
    expect(result.malformedLines).toEqual([1, 2]);
  });

  test.each([
    ["positional", "def f(ErrorCode): pass"],
    ["positional-only", "def f(ErrorCode, /): pass"],
    ["keyword-only", "def f(*, ErrorCode): pass"],
    ["variadic positional", "def f(*ErrorCode): pass"],
    ["variadic keyword", "def f(**ErrorCode): pass"],
  ])(
    "treats a %s ErrorCode parameter as shadowing",
    (_description, definition) => {
      const result = analyzeCodeBlockErrorCodes(
        `${definition}\nraise ErrorCode('c', 'r')`,
        { c: "declared" },
      );

      expect(result.declaredAndRaised).toEqual([]);
      expect(result.declaredButUnused).toEqual(["c"]);
      expect(result.malformedLines).toEqual([1, 2]);
    },
  );

  test("treats a lambda ErrorCode parameter as shadowing", () => {
    const result = analyzeCodeBlockErrorCodes(
      "callback = lambda ErrorCode: ErrorCode\nraise ErrorCode('c', 'r')",
      { c: "declared" },
    );

    expect(result.declaredAndRaised).toEqual([]);
    expect(result.declaredButUnused).toEqual(["c"]);
    expect(result.malformedLines).toEqual([1, 2]);
  });

  test("does not treat a similarly named parameter as ErrorCode", () => {
    const result = analyzeCodeBlockErrorCodes(
      "def f(ErrorCodeFoo): pass\nraise ErrorCode('c', 'r')",
      { c: "declared" },
    );

    expect(result.declaredAndRaised).toEqual([{ code: "c", lines: [2] }]);
    expect(result.declaredButUnused).toEqual([]);
    expect(result.malformedLines).toEqual([]);
  });

  test.each([
    [
      "exception handler alias",
      "try:\n  pass\nexcept Exception as ErrorCode:\n  pass",
      3,
    ],
    ["with target", "with context_manager() as ErrorCode:\n  pass", 1],
    ["match capture", "match value:\n  case ErrorCode:\n    pass", 2],
    [
      "match starred capture",
      "match value:\n  case [*ErrorCode]:\n    pass",
      2,
    ],
    [
      "match mapping rest capture",
      'match value:\n  case {"key": item, **ErrorCode}:\n    pass',
      2,
    ],
    [
      "match literal as-pattern",
      "match value:\n  case 1 as ErrorCode:\n    pass",
      2,
    ],
    [
      "match value as-pattern",
      "match value:\n  case some.ErrorCode as ErrorCode:\n    pass",
      2,
    ],
    [
      "match sequence as-pattern",
      "match value:\n  case [x] as ErrorCode:\n    pass",
      2,
    ],
  ])("treats an ErrorCode %s as shadowing", (_description, source, line) => {
    const result = analyzeCodeBlockErrorCodes(source, null);

    expect(result.declaredAndRaised).toEqual([]);
    expect(result.raisedButUndeclared).toEqual([]);
    expect(result.malformedLines).toEqual([line]);
  });

  test("allows an as-pattern that binds another name", () => {
    const result = analyzeCodeBlockErrorCodes(
      "match value:\n  case 1 as other:\n    pass",
      null,
    );

    expect(result.malformedLines).toEqual([]);
  });

  test.each([
    ["bare exception type", "except ErrorCode:"],
    ["exception type tuple", "except (ValueError, ErrorCode):"],
  ])("allows ErrorCode as a %s", (_description, handler) => {
    const result = analyzeCodeBlockErrorCodes(
      `try:\n  pass\n${handler}\n  pass`,
      null,
    );

    expect(result.malformedLines).toEqual([]);
  });

  test.each([
    ["default value reference", "def f(x=ErrorCode): pass", false],
    ["defaulted parameter", "def f(ErrorCode=x): pass", true],
    ["annotated parameter", "def f(ErrorCode: str): pass", true],
    ["annotation reference", "def f(x: ErrorCode): pass", false],
  ])(
    "classifies an ErrorCode %s by binding position",
    (_description, definition, shadows) => {
      const result = analyzeCodeBlockErrorCodes(
        `${definition}\nraise ErrorCode('c', 'r')`,
        { c: "declared" },
      );

      expect(result.declaredAndRaised).toEqual(
        shadows ? [] : [{ code: "c", lines: [2] }],
      );
      expect(result.declaredButUnused).toEqual(shadows ? ["c"] : []);
      expect(result.malformedLines).toEqual(shadows ? [1, 2] : []);
    },
  );

  test.each([
    ["import", "from package import ErrorCode"],
    ["assignment", "if condition:\n    ErrorCode = replacement"],
    ["class", "class ErrorCode: pass"],
    ["function", "def ErrorCode(): pass"],
    ["parameter", "def f(ErrorCode): pass"],
    ["import alias", "from package import Other as ErrorCode"],
  ])(
    "reports a no-raise %s ErrorCode binding on its binding line",
    (_description, source) => {
      const result = analyzeCodeBlockErrorCodes(source, null);

      expect(result.declaredAndRaised).toEqual([]);
      expect(result.raisedButUndeclared).toEqual([]);
      expect(result.malformedLines).toEqual([
        source.split("\n").findIndex((line) => line.includes("ErrorCode")) + 1,
      ]);
    },
  );

  test("leaves a block without ErrorCode bindings or raises clean", () => {
    expect(analyzeCodeBlockErrorCodes("def f(x):\n    return x", null)).toEqual(
      {
        declaredAndRaised: [],
        declaredButUnused: [],
        raisedButUndeclared: [],
        malformedLines: [],
      },
    );
  });

  test("reports a bare ErrorCode alias assignment without a raise", () => {
    const result = analyzeCodeBlockErrorCodes("Alias = ErrorCode", null);

    expect(result.malformedLines).toEqual([1]);
  });

  test("reports an ErrorCode alias binding and its later malformed raise once each", () => {
    const result = analyzeCodeBlockErrorCodes(
      "Alias = ErrorCode\nraise Alias('c', 'r')",
      null,
    );

    expect(result.malformedLines).toEqual([1, 2]);
  });

  test("does not report similarly named aliases", () => {
    const result = analyzeCodeBlockErrorCodes("AliasFoo = ErrorCodeFoo", null);

    expect(result.malformedLines).toEqual([]);
  });

  test("reports ErrorCode construction assigned outside a raise", () => {
    const result = analyzeCodeBlockErrorCodes(
      "Alias = ErrorCode('c', 'r')\nraise Alias('other', 'reason')",
      null,
    );

    expect(result.malformedLines).toEqual([1]);
  });

  test("marks multiple raises on one line malformed instead of declared", () => {
    const result = analyzeCodeBlockErrorCodes(
      "raise x; raise ErrorCode('c', 'r')",
      { c: "declared" },
    );

    expect(result.declaredAndRaised).toEqual([]);
    expect(result.declaredButUnused).toEqual(["c"]);
    expect(result.raisedButUndeclared).toEqual([]);
    expect(result.malformedLines).toEqual([1]);
  });

  test("marks raises with overlapping inclusive line spans malformed", () => {
    const result = analyzeCodeBlockErrorCodes(
      [
        "raise ErrorCode(",
        '    "A",',
        '    "why"); raise ErrorCode("B", "why")',
      ].join("\n"),
      { A: "declared", B: "declared" },
    );

    expect(result.declaredAndRaised).toEqual([]);
    expect(result.declaredButUnused).toEqual(["A", "B"]);
    expect(result.raisedButUndeclared).toEqual([]);
    expect(result.malformedLines).toEqual([1, 3]);
  });

  test("keeps a single multiline raise declared on its starting line", () => {
    const result = analyzeCodeBlockErrorCodes(
      ["raise ErrorCode(", '    "A",', '    "why")'].join("\n"),
      { A: "declared" },
    );

    expect(result.declaredAndRaised).toEqual([{ code: "A", lines: [1] }]);
    expect(result.declaredButUnused).toEqual([]);
    expect(result.raisedButUndeclared).toEqual([]);
    expect(result.malformedLines).toEqual([]);
  });

  test("handles raises on separate non-overlapping lines independently", () => {
    const result = analyzeCodeBlockErrorCodes(
      ['raise ErrorCode("A", "why")', 'raise ErrorCode("B", "why")'].join("\n"),
      { A: "declared", B: "declared" },
    );

    expect(result.declaredAndRaised).toEqual([
      { code: "A", lines: [1] },
      { code: "B", lines: [2] },
    ]);
    expect(result.declaredButUnused).toEqual([]);
    expect(result.raisedButUndeclared).toEqual([]);
    expect(result.malformedLines).toEqual([]);
  });

  test("keeps a single direct raise on its own line declared", () => {
    const result = analyzeCodeBlockErrorCodes("raise ErrorCode('c', 'r')", {
      c: "declared",
    });

    expect(result.declaredAndRaised).toEqual([{ code: "c", lines: [1] }]);
    expect(result.malformedLines).toEqual([]);
  });

  test.each([
    ["attribute assignment", "x.ErrorCode = replacement"],
    ["nested-call keyword", "consume(ErrorCode=replacement)"],
  ])("does not treat %s as an ErrorCode parameter", (_description, setup) => {
    const result = analyzeCodeBlockErrorCodes(
      `${setup}\nraise ErrorCode('c', 'r')`,
      { c: "declared" },
    );

    expect(result.declaredAndRaised).toEqual([{ code: "c", lines: [2] }]);
    expect(result.declaredButUnused).toEqual([]);
    expect(result.malformedLines).toEqual([]);
  });
});
