import type { Node } from "@xyflow/react";

import type { WorkflowParameterValueType } from "../types/workflowTypes";
import type { ParametersState } from "./types";

type DeletedInput = Pick<ParametersState[number], "key" | "parameterType"> & {
  dataType?: WorkflowParameterValueType;
};

/**
 * Pure helpers for detecting, transforming, and walking Jinja-style
 * (`{{ key }}`) references inside block data. Kept in a standalone module so
 * test code — and sibling editor utilities that only need the reference
 * logic — can import these without transitively loading the full editor
 * runtime (node type registry, AxiosClient, React Query hooks, etc.).
 */

/**
 * Escapes special regex characters in a string.
 */
function escapeRegExp(string: string): string {
  return string.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Replaces jinja-style references to a variable name.
 * Handles patterns like {{oldKey}}, {{oldKey.field}}, {{oldKey | filter}}
 * @param text - The text to search in
 * @param oldKey - The key to replace (without braces)
 * @param newKey - The new key to use (without braces)
 * @returns The text with references replaced
 */
export function replaceJinjaReference(
  text: string,
  oldKey: string,
  newKey: string,
): string {
  // Match {{oldKey}} or {{oldKey.something}} or {{oldKey | filter}} or {{oldKey[0]}} etc.
  // Use negative lookahead to ensure key is not followed by identifier characters,
  // which prevents matching {{keyOther}} when searching for {{key}}
  // Capture whitespace after {{ to preserve formatting (e.g., "{{ key }}" stays "{{ newKey }}")
  const regex = new RegExp(
    `\\{\\{(\\s*)${escapeRegExp(oldKey)}(?![a-zA-Z0-9_])`,
    "g",
  );
  return text.replace(regex, (_, whitespace) => `{{${whitespace}${newKey}`);
}

/**
 * Removes jinja-style references from a string.
 * Handles patterns like {{key}}, {{key.field}}, {{key | filter}}
 * @param text - The text to search in
 * @param key - The key to remove (without braces)
 * @returns The text with references removed
 */
// Cap the inside-braces match at 500 chars: bounded prevents catastrophic
// backtracking on adversarial input. Real Jinja expressions in this editor
// (`{{ key.field | filter:arg }}`) are well under that bound; on the rare
// occurrence that one exceeds it, the reference simply stays in place
// rather than the regex engine stalling. The deletion path is best-effort:
// callers don't depend on every reference being removed.
const MAX_JINJA_INTERIOR_CHARS = 500;

export function removeJinjaReference(text: string, key: string): string {
  // Capture whitespace adjacent to the reference so the replacement can
  // collapse the gap left behind without globally rewriting the field.
  // Globally collapsing was a real bug: a multiline prompt with
  // intentional `\n\n` paragraph breaks would have those breaks flattened
  // every time an unrelated block/parameter was deleted.
  // Interior pattern allows a single `}` (e.g. `default('{}')`); only
  // `}}` terminates, since that is the closing delimiter.
  const regex = new RegExp(
    `(\\s*)\\{\\{\\s*${escapeRegExp(key)}(?![a-zA-Z0-9_])(?:[^}]|}(?!})){0,${MAX_JINJA_INTERIOR_CHARS}}\\}\\}(\\s*)`,
    "g",
  );
  return text.replace(
    regex,
    (match, leading: string, trailing: string, offset: number) => {
      const atStart = offset === 0;
      const atEnd = offset + match.length === text.length;
      if (atStart && atEnd) return "";
      // At an edge, preserve the user's outer whitespace; only the
      // captured-on-the-other-side run is dropped along with the reference.
      if (atStart) return leading;
      if (atEnd) return trailing;
      // Mid-string: collapse the gap to a single separator so words/lines
      // adjoining the removed reference don't fuse. Prefer a newline when
      // either side had one so multi-line layout is preserved.
      if (leading.includes("\n") || trailing.includes("\n")) return "\n";
      if (leading.length > 0 || trailing.length > 0) return " ";
      return "";
    },
  );
}

/**
 * Checks if a string contains a jinja reference to a specific key.
 */
export function containsJinjaReference(text: string, key: string): boolean {
  // Use negative lookahead to ensure key is not followed by identifier characters
  const regex = new RegExp(`\\{\\{\\s*${escapeRegExp(key)}(?![a-zA-Z0-9_])`);
  return regex.test(text);
}

const PYTHON_STRING_OR_COMMENT =
  /("""|''')[\s\S]*?(?:\1|$)|"(?:[^"\\\n]|\\.)*"?|'(?:[^'\\\n]|\\.)*'?|#[^\n]*/g;

export function stripPythonComments(code: string): string {
  return code.replace(PYTHON_STRING_OR_COMMENT, (match) =>
    match.startsWith("#") ? "" : match,
  );
}

const JINJA_RAW_BLOCK =
  /\{%[-+]?\s*raw\s*[-+]?%\}[\s\S]*?(?:\{%[-+]?\s*endraw\s*[-+]?%\}|$)/g;
const JINJA_SPAN = /\{\{([\s\S]*?)\}\}|\{%([\s\S]*?)%\}/g;
const QUOTED_STRING = /"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'/g;
const JINJA_KEYWORDS = new Set([
  "and",
  "or",
  "not",
  "in",
  "is",
  "if",
  "else",
  "recursive",
  "true",
  "false",
  "none",
  "True",
  "False",
  "None",
]);
// A name read as a value: not an attribute, filter, test or keyword-argument name.
const JINJA_ROOT_NAME =
  /(?<!\w)(?<!\.\s*)(?<!\|\s*)(?<!\bis\s+)(?<!\bis\s+not\s+)[A-Za-z_]\w*(?!\w)(?!\s*=(?!=))/g;
const FOR_TAG = /^for\s+([\w\s,()]+?)\s+in\b/;
const SET_TAG = /^set\s+([\w\s,]+?)\s*(?:=[\s\S]*)?$/;

function splitNames(targets: string): Array<string> {
  return targets
    .replace(/[()]/g, "")
    .split(",")
    .map((name) => name.trim())
    .filter(Boolean);
}

// Control flow is not simulated: a for target is bound until its endfor, and a set binds from its
// tag onward only outside any if/for, since inside one it may never run.
function jinjaReads(code: string): {
  outputReads: Array<string>;
  tagReads: Array<string>;
} {
  const outputReads: Array<string> = [];
  const tagReads: Array<string> = [];
  const scopes: Array<{ tag: "for" | "if"; names: Array<string> }> = [];
  const topLevelSets = new Set<string>();
  const unbound = (text: string) => {
    const bound = new Set([
      ...topLevelSets,
      ...scopes.flatMap((scope) => scope.names),
    ]);
    return (text.match(JINJA_ROOT_NAME) ?? []).filter(
      (name) => !JINJA_KEYWORDS.has(name) && !bound.has(name),
    );
  };
  for (const [, output, tag] of stripPythonComments(code)
    .replace(JINJA_RAW_BLOCK, "")
    .matchAll(JINJA_SPAN)) {
    if (output !== undefined) {
      outputReads.push(...unbound(output.replace(QUOTED_STRING, '""')));
      continue;
    }
    const statement = (tag ?? "")
      .replace(QUOTED_STRING, '""')
      .trim()
      .replace(/^[-+]|[-+]$/g, "")
      .trim();
    tagReads.push(...unbound(statement.replace(FOR_TAG, "")));
    const keyword = statement.match(/^\w+/)?.[0];
    if (keyword === "for") {
      scopes.push({
        tag: "for",
        names: [...splitNames(FOR_TAG.exec(statement)?.[1] ?? ""), "loop"],
      });
    } else if (keyword === "if") {
      scopes.push({ tag: "if", names: [] });
    } else if (keyword === "endfor" || keyword === "endif") {
      const open = scopes
        .map((scope) => scope.tag)
        .lastIndexOf(keyword === "endfor" ? "for" : "if");
      if (open >= 0) {
        scopes.length = open;
      }
    } else if (keyword === "set") {
      const match = SET_TAG.exec(statement);
      if (match && scopes.length === 0) {
        splitNames(match[1] ?? "").forEach((name) => topLevelSets.add(name));
      }
    }
  }
  return { outputReads, tagReads };
}

// Lists names read inside {{ }} only, matching what the AI fallback receives.
export function codeBlockUses(
  parameterKeys: ReadonlyArray<string>,
  code: string,
): Array<string> {
  return Array.from(
    new Set([...parameterKeys, ...jinjaReads(code).outputReads]),
  );
}

export type CodeBlockUseKind = "input" | "block" | "neither";

export function classifyUse(
  name: string,
  inputKeys: ReadonlySet<string>,
  blockLabels: ReadonlySet<string>,
): CodeBlockUseKind {
  if (inputKeys.has(name)) {
    return "input";
  }
  if (blockLabels.has(name) || blockLabels.has(name.replace(/_output$/, ""))) {
    return "block";
  }
  return "neither";
}

/**
 * Recursively checks if any string field in an object contains a jinja
 * reference to a key. Mirrors the shape used by the editor's existing ref
 * scanner so nested payloads (loop prompts, HTTP headers, etc.) are covered
 * — not just top-level string fields.
 */
export function objectContainsJinjaReference(
  obj: unknown,
  key: string,
  skipKeys: Set<string>,
  depth: number = 0,
): boolean {
  const MAX_DEPTH = 50;
  if (depth > MAX_DEPTH || obj === null || obj === undefined) {
    return false;
  }

  if (typeof obj === "string") {
    return containsJinjaReference(obj, key);
  }

  if (Array.isArray(obj)) {
    return obj.some((item) =>
      objectContainsJinjaReference(item, key, skipKeys, depth + 1),
    );
  }

  if (typeof obj === "object") {
    for (const [objKey, value] of Object.entries(obj)) {
      if (skipKeys.has(objKey)) {
        continue;
      }
      if (objectContainsJinjaReference(value, key, skipKeys, depth + 1)) {
        return true;
      }
    }
  }

  return false;
}

// Keys never searched or rewritten for jinja references.
export const SKIP_KEYS_FOR_JINJA_CHECK = new Set([
  "label",
  "key",
  "type",
  "id",
  "nodeId",
  "parameterKeys",
]);

const MAX_TRANSFORM_DEPTH = 50;

export function transformStringFieldsInObject<T>(
  obj: T,
  transform: (value: string) => string,
  skipKeys: Set<string>,
  depth: number = 0,
): T {
  if (depth > MAX_TRANSFORM_DEPTH || obj === null || obj === undefined) {
    return obj;
  }

  if (typeof obj === "string") {
    return transform(obj) as T;
  }

  if (Array.isArray(obj)) {
    return obj.map((item) =>
      transformStringFieldsInObject(item, transform, skipKeys, depth + 1),
    ) as T;
  }

  if (typeof obj === "object") {
    const result: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(obj)) {
      result[key] = skipKeys.has(key)
        ? value
        : transformStringFieldsInObject(value, transform, skipKeys, depth + 1);
    }
    return result as T;
  }

  return obj;
}

// A delete never edits code, and the backend rebuilds steps from code on save, so both are left.
const SKIP_KEYS_FOR_CODE_BLOCK_REMOVAL = new Set([
  ...SKIP_KEYS_FOR_JINJA_CHECK,
  "code",
  "steps",
]);

// ponytail: a text that binds the name is left whole, so a read of the deleted item outside the
// binding's scope in that same text is kept too; track scopes per reference if that shows up.
function templateBindsName(text: string, name: string): boolean {
  return Array.from(text.matchAll(JINJA_SPAN)).some(([, , tag]) => {
    const statement = (tag ?? "").trim().replace(/^[-+]\s*/, "");
    const targets =
      FOR_TAG.exec(statement)?.[1] ?? SET_TAG.exec(statement)?.[1] ?? "";
    return splitNames(targets).includes(name);
  });
}

/**
 * Removes {{ }} references to any of `keys` from every node's string fields except a code block's
 * code and steps. `onChange` sees each changed string with the top-level field it sits under.
 */
export function removeJinjaReferenceFromNodes<T extends Node>(
  nodes: T[],
  keys: ReadonlyArray<string>,
  onChange?: (
    nodeId: string,
    field: string,
    before: string,
    after: string,
  ) => void,
): T[] {
  return nodes.map((node) => {
    if (!node.data) {
      return node;
    }
    const skipKeys =
      node.type === "codeBlock"
        ? SKIP_KEYS_FOR_CODE_BLOCK_REMOVAL
        : SKIP_KEYS_FOR_JINJA_CHECK;
    const data: Record<string, unknown> = {};
    for (const [field, value] of Object.entries(node.data)) {
      data[field] = skipKeys.has(field)
        ? value
        : transformStringFieldsInObject(
            value,
            (before) => {
              const after = keys.reduce(
                (text, key) =>
                  templateBindsName(text, key)
                    ? text
                    : removeJinjaReference(text, key),
                before,
              );
              if (after !== before) {
                onChange?.(node.id, field, before, after);
              }
              return after;
            },
            skipKeys,
            1,
          );
    }
    return { ...node, data } as T;
  });
}

// Topological order: a parent always precedes its children.
export function descendants<T extends Node>(
  nodes: Array<T>,
  id: string,
): Array<T> {
  const children = nodes.filter((n) => n.parentId === id);
  return children.concat(...children.map((c) => descendants(nodes, c.id)));
}

function isReferenceBlock(node: Node): boolean {
  return (
    !!node.data &&
    "label" in node.data &&
    node.type !== "start" &&
    node.type !== "nodeAdder"
  );
}

export type ReferenceTarget = {
  parameterKeys: ReadonlyArray<string>;
  jinjaKeys: ReadonlyArray<string>;
  // Deleted input keys a code block binds as a secret or credential.
  credentialKeys?: ReadonlyArray<string>;
  // Deleted labels that an input shares, so their {{ }} references are kept and read the input.
  inputShadowedKeys?: ReadonlyArray<string>;
};

// Mirrors the backend's code-block binding: every input except a plain workflow or context
// parameter is a secret or credential.
export function inputDeleteTarget(input: DeletedInput): ReferenceTarget {
  const isCredential =
    input.parameterType === "workflow"
      ? input.dataType === "credential_id"
      : input.parameterType !== "context";
  return {
    parameterKeys: [input.key],
    jinjaKeys: [input.key],
    credentialKeys: isCredential ? [input.key] : [],
  };
}

/**
 * Keys a block delete removes for the block and its descendants: each `<label>_output` and raw
 * label, except in {{ }} text where an input or a surviving block has that name.
 */
export function blockDeleteTarget<T extends Node>(
  nodes: Array<T>,
  id: string,
  inputKeys: ReadonlySet<string>,
): ReferenceTarget & { deletedIds: Set<string> } {
  const deleted = nodes
    .filter((node) => node.id === id)
    .concat(descendants(nodes, id));
  const deletedIds = new Set(deleted.map((node) => node.id));
  const labels = deleted
    .filter(isReferenceBlock)
    .map((node) => node.data.label as string);
  const outputKeys = labels.map((label) => `${label}_output`);
  const survivingNames = new Set(
    nodes
      .filter((node) => !deletedIds.has(node.id) && isReferenceBlock(node))
      .flatMap((node) => [
        node.data.label as string,
        `${node.data.label as string}_output`,
      ]),
  );
  return {
    deletedIds,
    parameterKeys: outputKeys,
    jinjaKeys: [...outputKeys, ...labels].filter(
      (key) => !inputKeys.has(key) && !survivingNames.has(key),
    ),
    inputShadowedKeys: labels.filter((label) => inputKeys.has(label)),
  };
}

/**
 * Information about a block that references a parameter or block output.
 */
export type AffectedBlock = {
  nodeId: string;
  label: string;
  hasParameterKeyReference: boolean;
  hasJinjaReference: boolean;
  isCode: boolean;
  // Target keys a code block reads in its code, as a declared input's bare name or in {{ }}.
  codeReadKeys: Array<string>;
  // Declared credential keys the code is not seen to read; their field names are not known here.
  credentialKeys: Array<string>;
  loopValueCleared: boolean;
  // Target keys the block declares as inputs, reads in its code, or loops over.
  keys: Array<string>;
  // Each changed text as it reads after the delete, starting at the sentence of its first change.
  changedTexts: Array<string>;
  // Deleted labels the block references in {{ }} that will read a same-named input instead.
  inputShadowedKeys: Array<string>;
};

const F_STRING_PREFIX = /(?<!\w)(?:[fF][rR]?|[rR][fF])$/;

// A declared input runs as a Python variable named by its key. ponytail: no scope analysis, so a
// name rebound and then read later still counts as a read.
function codeReadsName(code: string, name: string): boolean {
  const masked = code.replace(
    PYTHON_STRING_OR_COMMENT,
    (match: string, _quote: string, offset: number) =>
      !match.startsWith("#") && F_STRING_PREFIX.test(code.slice(0, offset))
        ? (match.match(/\{(?:[^{}]|\{[^{}]*\})*\}/g) ?? []).join(" ")
        : "",
  );
  return new RegExp(
    `(?<![\\w.])${escapeRegExp(name)}(?!\\w)(?!\\s*=(?!=))`,
  ).test(masked);
}

const CHANGED_TEXT_EXCERPT_CHARS = 160;
const CHANGED_TEXT_LEAD_CHARS = 60;

function changedTextExcerpt(before: string, after: string): string {
  let firstChange = 0;
  while (
    firstChange < after.length &&
    before[firstChange] === after[firstChange]
  ) {
    firstChange++;
  }
  const sentenceStart = after
    .slice(0, Math.min(firstChange, after.length - 1))
    .search(/[^.!?\n]*$/);
  const start = Math.max(sentenceStart, firstChange - CHANGED_TEXT_LEAD_CHARS);
  const text = `${start > sentenceStart ? "..." : ""}${after.slice(start).trimStart()}`;
  return text.length > CHANGED_TEXT_EXCERPT_CHARS
    ? `${text.slice(0, CHANGED_TEXT_EXCERPT_CHARS).trimEnd()}...`
    : text;
}

/**
 * Finds all blocks that reference a key (parameter or block output), or a delete target.
 * A string key keeps the plain scan; a target also reads a code block's code as the runtime does.
 */
export function getAffectedBlocks<T extends Node>(
  nodes: T[],
  target: string | ReferenceTarget,
): AffectedBlock[] {
  const isDeleteTarget = typeof target !== "string";
  const {
    parameterKeys: targetParameterKeys,
    jinjaKeys,
    credentialKeys: targetCredentialKeys = [],
    inputShadowedKeys: targetShadowedKeys = [],
  }: ReferenceTarget = isDeleteTarget
    ? target
    : { parameterKeys: [target], jinjaKeys: [target] };

  const changedTexts = new Map<string, Array<string>>();
  if (isDeleteTarget) {
    removeJinjaReferenceFromNodes(
      nodes,
      jinjaKeys,
      (nodeId, field, before, after) => {
        if (field !== "goalBeforeEdit") {
          changedTexts.set(nodeId, [
            ...(changedTexts.get(nodeId) ?? []),
            changedTextExcerpt(before, after),
          ]);
        }
      },
    );
  }

  const affectedBlocks: AffectedBlock[] = [];

  for (const node of nodes) {
    if (!isReferenceBlock(node)) {
      continue;
    }

    const isCode = node.type === "codeBlock";
    const scanCode = isDeleteTarget && isCode;
    const parameterKeys = (node.data.parameterKeys ?? []) as Array<string>;
    const declaredKeys = targetParameterKeys.filter((key) =>
      parameterKeys.includes(key),
    );
    const code = scanCode ? ((node.data.code as string | undefined) ?? "") : "";
    const codeUses = scanCode ? Object.values(jinjaReads(code)).flat() : [];
    const codeKeys = jinjaKeys.filter((key) => codeUses.includes(key));
    const codeReadKeys = scanCode
      ? Array.from(
          new Set([
            ...codeKeys,
            ...declaredKeys.filter((key) => codeReadsName(code, key)),
          ]),
        )
      : [];
    const credentialKeys = scanCode
      ? declaredKeys.filter(
          (key) =>
            targetCredentialKeys.includes(key) && !codeReadKeys.includes(key),
        )
      : [];
    const loopValue =
      node.type === "loop"
        ? ((node.data.loopVariableReference as string | undefined) ?? "")
        : "";
    const loopValueCleared =
      loopValue !== "" && targetParameterKeys.includes(loopValue);

    const hasParameterKeyReference = declaredKeys.length > 0;
    const inputShadowedKeys = targetShadowedKeys.filter((key) =>
      objectContainsJinjaReference(node.data, key, SKIP_KEYS_FOR_JINJA_CHECK),
    );
    const hasJinjaReference =
      loopValueCleared ||
      codeKeys.length > 0 ||
      inputShadowedKeys.length > 0 ||
      (isDeleteTarget
        ? changedTexts.has(node.id)
        : objectContainsJinjaReference(
            node.data,
            target,
            SKIP_KEYS_FOR_JINJA_CHECK,
          ));

    if (hasParameterKeyReference || hasJinjaReference) {
      affectedBlocks.push({
        nodeId: node.id,
        label: node.data.label as string,
        hasParameterKeyReference,
        hasJinjaReference,
        isCode,
        codeReadKeys,
        credentialKeys,
        loopValueCleared,
        keys: Array.from(
          new Set([
            ...declaredKeys,
            ...codeKeys,
            ...(loopValueCleared ? [loopValue] : []),
          ]),
        ),
        changedTexts: changedTexts.get(node.id) ?? [],
        inputShadowedKeys,
      });
    }
  }

  return affectedBlocks;
}
