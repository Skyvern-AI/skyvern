import { pythonLanguage } from "@codemirror/lang-python";

type SyntaxNode = {
  name: string;
  from: number;
  to: number;
  firstChild: SyntaxNode | null;
  nextSibling: SyntaxNode | null;
  getChild: (name: string) => SyntaxNode | null;
};

export type RaisedErrorCodeDiagnostic = { code: string; lines: Array<number> };

export type CodeBlockErrorCodeDiagnostics = {
  declaredAndRaised: Array<RaisedErrorCodeDiagnostic>;
  declaredButUnused: Array<string>;
  raisedButUndeclared: Array<RaisedErrorCodeDiagnostic>;
  malformedLines: Array<number>;
};

function children(node: SyntaxNode): Array<SyntaxNode> {
  const result: Array<SyntaxNode> = [];
  for (let child = node.firstChild; child; child = child.nextSibling) {
    result.push(child);
  }
  return result;
}

function lineOffsets(source: string): Array<number> {
  const offsets = [0];
  for (let index = 0; index < source.length; index += 1) {
    if (source[index] === "\n") offsets.push(index + 1);
  }
  return offsets;
}

function lineAt(offsets: Array<number>, offset: number): number {
  let low = 0;
  let high = offsets.length;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (offsets[middle]! <= offset) low = middle + 1;
    else high = middle;
  }
  return low;
}

function errorCodeImportBindings(
  source: string,
  root: SyntaxNode,
): Set<string> {
  const bindings = new Set<string>();

  function visit(node: SyntaxNode): void {
    if (node.name === "ImportStatement") {
      const parts = children(node);
      const importIndex = parts.findIndex((part) => part.name === "import");
      const groups: Array<Array<SyntaxNode>> = [[]];
      for (const part of parts.slice(importIndex + 1)) {
        if (part.name === "(" || part.name === ")") continue;
        if (part.name === ",") groups.push([]);
        else groups[groups.length - 1]!.push(part);
      }

      for (const group of groups) {
        const names = group.filter((part) => part.name === "VariableName");
        const asIndex = group.findIndex((part) => part.name === "as");
        const boundName =
          asIndex >= 0
            ? group
                .slice(asIndex + 1)
                .find((part) => part.name === "VariableName")
            : names[0];
        if (
          boundName &&
          source.slice(boundName.from, boundName.to) === "ErrorCode"
        ) {
          bindings.add("ErrorCode");
        }
      }
      return;
    }
    for (const child of children(node)) visit(child);
  }

  visit(root);
  return bindings;
}

function errorCodeBindingLines(
  source: string,
  root: SyntaxNode,
  offsets: Array<number>,
): Array<number> {
  const bindingLines = new Set<number>();

  function addIfErrorCode(node: SyntaxNode | undefined): void {
    if (
      node?.name === "VariableName" &&
      source.slice(node.from, node.to) === "ErrorCode"
    ) {
      bindingLines.add(lineAt(offsets, node.from));
    }
  }

  function addTargetNames(node: SyntaxNode): void {
    if (node.name === "VariableName") {
      addIfErrorCode(node);
      return;
    }
    if (node.name === "TypeDef" || node.name === "MemberExpression") return;
    for (const child of children(node)) addTargetNames(child);
  }

  function visit(node: SyntaxNode): void {
    const parts = children(node);
    if (node.name === "ImportStatement") {
      const importIndex = parts.findIndex((part) => part.name === "import");
      const groups: Array<Array<SyntaxNode>> = [[]];
      for (const part of parts.slice(importIndex + 1)) {
        if (part.name === "(" || part.name === ")") continue;
        if (part.name === ",") groups.push([]);
        else groups[groups.length - 1]!.push(part);
      }
      for (const group of groups) {
        const asIndex = group.findIndex((part) => part.name === "as");
        addIfErrorCode(
          asIndex >= 0
            ? group
                .slice(asIndex + 1)
                .find((part) => part.name === "VariableName")
            : group.find((part) => part.name === "VariableName"),
        );
      }
      return;
    }
    if (node.name === "FunctionDefinition" || node.name === "ClassDefinition") {
      addIfErrorCode(parts.find((part) => part.name === "VariableName"));
    } else if (node.name === "ParamList") {
      let afterAssign = false;
      for (const part of parts) {
        if (part.name === ",") afterAssign = false;
        else if (part.name === "AssignOp") afterAssign = true;
        else if (!afterAssign && part.name === "VariableName") {
          addIfErrorCode(part);
        }
      }
    } else if (node.name === "AssignStatement") {
      let finalAssignIndex = -1;
      for (let index = parts.length - 1; index >= 0; index -= 1) {
        if (parts[index]!.name === "AssignOp") {
          finalAssignIndex = index;
          break;
        }
      }
      const targetParts =
        finalAssignIndex < 0
          ? parts.slice(0, 1)
          : parts.slice(0, finalAssignIndex);
      for (const part of targetParts) {
        if (part.name !== "AssignOp") addTargetNames(part);
      }
    } else if (
      node.name === "UpdateStatement" ||
      node.name === "NamedExpression"
    ) {
      if (parts[0]) addTargetNames(parts[0]);
    } else if (node.name === "ForStatement") {
      const inIndex = parts.findIndex((part) => part.name === "in");
      for (const part of parts.slice(1, inIndex)) addTargetNames(part);
    } else if (node.name === "WithStatement") {
      for (let index = 0; index < parts.length - 1; index += 1) {
        if (parts[index]!.name === "as") addTargetNames(parts[index + 1]!);
      }
    } else if (node.name === "TryStatement") {
      for (let index = 0; index < parts.length - 1; index += 1) {
        if (parts[index]!.name === "as") addTargetNames(parts[index + 1]!);
      }
    } else if (node.name === "AsPattern") {
      for (let index = 0; index < parts.length - 1; index += 1) {
        if (parts[index]!.name === "as") addTargetNames(parts[index + 1]!);
      }
    } else if (node.name === "CapturePattern") {
      addIfErrorCode(parts.find((part) => part.name === "VariableName"));
    } else if (node.name.endsWith("ComprehensionExpression")) {
      for (let index = 0; index < parts.length - 1; index += 1) {
        if (parts[index]!.name === "for") addTargetNames(parts[index + 1]!);
      }
    }
    for (const child of parts) visit(child);
  }

  visit(root);
  return Array.from(bindingLines).sort((left, right) => left - right);
}

function decodeString(source: string): string | null {
  const match = source.match(/^([rRuU]*)("""|'''|"|')([\s\S]*)(\2)$/);
  if (!match) return null;
  const raw = match[1]!.toLowerCase().includes("r");
  const body = match[3]!;
  if (raw) return body;
  try {
    const continuedBody = body.replace(/\\(?:\r\n|\n|\r)/g, "");
    return continuedBody.replace(
      /\\(?:x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|[0-7]{1,3}|[\\'"abfnrtv])/g,
      (escape) => {
        const value = escape.slice(1);
        if (value[0] === "x" || value[0] === "u" || value[0] === "U") {
          return String.fromCodePoint(Number.parseInt(value.slice(1), 16));
        }
        if (/^[0-7]+$/.test(value)) {
          return String.fromCodePoint(Number.parseInt(value, 8));
        }
        const simple: Record<string, string> = {
          "\\": "\\",
          "'": "'",
          '"': '"',
          a: "\u0007",
          b: "\b",
          f: "\f",
          n: "\n",
          r: "\r",
          t: "\t",
          v: "\u000b",
        };
        return simple[value] ?? escape;
      },
    );
  } catch {
    return null;
  }
}

function directLiteralCode(source: string, call: SyntaxNode): string | null {
  const argList = call.getChild("ArgList");
  if (!argList) return null;
  const argNodes = children(argList);
  if (
    argNodes.some(
      (node) =>
        node.name === "AssignOp" && source.slice(node.from, node.to) === "=",
    )
  )
    return null;
  const groups: Array<Array<SyntaxNode>> = [[]];
  for (const child of argNodes) {
    if (child.name === "(" || child.name === ")") continue;
    if (child.name === ",") groups.push([]);
    else groups[groups.length - 1]!.push(child);
  }
  if (groups[groups.length - 1]?.length === 0) groups.pop();
  if (groups.length !== 2 || groups[0]!.length === 0) return null;
  const literalNodes = groups[0]!.flatMap((node) =>
    node.name === "ContinuedString" ? children(node) : [node],
  );
  if (literalNodes.some((node) => node.name !== "String")) return null;
  const decoded = literalNodes.map((node) =>
    decodeString(source.slice(node.from, node.to)),
  );
  if (decoded.some((value) => value === null)) return null;
  const code = decoded.join("");
  return code.length > 0 && code.length <= 128 && code === code.trim()
    ? code
    : null;
}

export function analyzeCodeBlockErrorCodes(
  code: string,
  effectiveManifest: Record<string, string> | null,
): CodeBlockErrorCodeDiagnostics {
  const raised = new Map<string, Array<number>>();
  const malformedLines = new Set<number>();
  const aliases = new Set<string>();
  const offsets = lineOffsets(code);
  const tree = pythonLanguage.parser.parse(code);
  const root = tree.topNode as unknown as SyntaxNode;
  const bindingLines = errorCodeBindingLines(code, root, offsets);
  for (const line of bindingLines) malformedLines.add(line);
  const importBindings = errorCodeImportBindings(code, root);
  const importBindsErrorCode = importBindings.has("ErrorCode");
  for (const binding of importBindings) {
    if (binding !== "ErrorCode") aliases.add(binding);
  }
  const errorCodeIsShadowed = importBindsErrorCode || bindingLines.length > 0;
  tree.iterate({
    enter(reference) {
      if (reference.name !== "AssignStatement") return;
      const assignment = reference.node as unknown as SyntaxNode;
      const parts = children(assignment);
      let finalAssignIndex = -1;
      for (let index = parts.length - 1; index >= 0; index -= 1) {
        if (parts[index]!.name === "AssignOp") {
          finalAssignIndex = index;
          break;
        }
      }
      const value = parts[finalAssignIndex + 1];
      const isBareAlias =
        value?.name === "VariableName" &&
        code.slice(value.from, value.to) === "ErrorCode";
      const isConstruction =
        value?.name === "CallExpression" &&
        value.firstChild !== null &&
        code.slice(value.firstChild.from, value.firstChild.to) === "ErrorCode";
      if (isBareAlias && parts[0]?.name === "VariableName") {
        aliases.add(code.slice(parts[0].from, parts[0].to));
      }
      if (isBareAlias || isConstruction) {
        malformedLines.add(lineAt(offsets, assignment.from));
      }
    },
  });

  const raisesPerLine = new Map<number, number>();
  tree.iterate({
    enter(reference) {
      if (reference.name !== "RaiseStatement") return;
      const startLine = lineAt(offsets, reference.from);
      const endLine = lineAt(offsets, reference.to);
      for (let line = startLine; line <= endLine; line += 1) {
        raisesPerLine.set(line, (raisesPerLine.get(line) ?? 0) + 1);
      }
    },
  });
  for (const [line, count] of raisesPerLine) {
    if (count > 1) malformedLines.add(line);
  }

  tree.iterate({
    enter(reference) {
      if (reference.name !== "RaiseStatement") return;
      const raiseNode = reference.node as unknown as SyntaxNode;
      const call = children(raiseNode).find(
        (node) => node.name === "CallExpression",
      );
      if (!call) return;
      const callee = call.firstChild;
      if (!callee) return;
      const calleeText = code.slice(callee.from, callee.to);
      const line = lineAt(offsets, raiseNode.from);
      const endLine = lineAt(offsets, raiseNode.to);
      let isAmbiguous = false;
      for (let coveredLine = line; coveredLine <= endLine; coveredLine += 1) {
        if (raisesPerLine.get(coveredLine) !== 1) {
          isAmbiguous = true;
          break;
        }
      }
      if (isAmbiguous) {
        malformedLines.add(line);
        return;
      }
      if (calleeText !== "ErrorCode") {
        if (calleeText.endsWith(".ErrorCode") || aliases.has(calleeText)) {
          malformedLines.add(line);
        }
        return;
      }
      if (errorCodeIsShadowed) {
        malformedLines.add(line);
        return;
      }
      const literalCode = directLiteralCode(code, call);
      if (literalCode === null) {
        malformedLines.add(line);
        return;
      }
      const lines = raised.get(literalCode) ?? [];
      if (!lines.includes(line)) lines.push(line);
      raised.set(literalCode, lines);
    },
  });

  const declared = Object.keys(effectiveManifest ?? {});
  const declaredSet = new Set(declared);
  return {
    declaredAndRaised: declared
      .filter((manifestCode) => raised.has(manifestCode))
      .map((manifestCode) => ({
        code: manifestCode,
        lines: raised.get(manifestCode)!,
      })),
    declaredButUnused: declared.filter(
      (manifestCode) => !raised.has(manifestCode),
    ),
    raisedButUndeclared: Array.from(raised)
      .filter(([raisedCode]) => !declaredSet.has(raisedCode))
      .map(([raisedCode, lines]) => ({ code: raisedCode, lines })),
    malformedLines: Array.from(malformedLines).sort(
      (left, right) => left - right,
    ),
  };
}
