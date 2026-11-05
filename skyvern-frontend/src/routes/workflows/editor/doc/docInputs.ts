import type { Node } from "@xyflow/react";
import { getInvalidJsonMessage } from "@/util/jsonParseError";
import {
  WorkflowParameterValueType,
  type WorkflowParameterValueType as ValueType,
} from "../../types/workflowTypes";
import type { ParametersState } from "../types";
import {
  RESERVED_PARAMETER_KEYS,
  renameParameterInNodes,
  replaceParameter,
  validateParameterKey,
} from "../parameterEdits";
import { removeKeyFromNodesParameterKeys } from "../workflowEditorUtils";

export type WorkflowParameter = Extract<
  ParametersState[number],
  { parameterType: "workflow" }
>;

export type DocInputForm = {
  key: string;
  dataType: ValueType;
  defaultText: string;
  description: string;
};

export function isDocEditableInput(
  parameter: ParametersState[number],
): parameter is ParametersState[number] {
  return Boolean(parameter);
}

export function docInputForm(parameter: WorkflowParameter): DocInputForm {
  const value = parameter.defaultValue;
  return {
    key: parameter.key,
    dataType: parameter.dataType,
    defaultText:
      value === null || value === undefined
        ? ""
        : typeof value === "string" &&
            parameter.dataType !== WorkflowParameterValueType.JSON
          ? value
          : JSON.stringify(value),
    description: parameter.description ?? "",
  };
}

export function isDocInputKeyTaken(
  key: string,
  nodes: Node[],
  otherKeys: string[],
): boolean {
  if (otherKeys.includes(key)) return true;
  return nodes.some(
    (n) => `${(n.data as { label?: unknown }).label}_output` === key,
  );
}

/** Why a name can't be used for an input, or null. A reserved name is only refused when it is being newly taken. */
export function docInputNameProblem(
  key: string,
  oldKey: string | null,
  nodes: Node[],
  otherKeys: string[],
): string | null {
  if (key !== oldKey && RESERVED_PARAMETER_KEYS.includes(key))
    return `${key} is reserved. Use another name.`;
  if (isDocInputKeyTaken(key, nodes, otherKeys))
    return "Another input or step result already has this name.";
  return null;
}

/** A name like `new_input` no other input or step output uses. */
export function newDocInputKey(
  parameters: ParametersState,
  nodes: Node[],
): string {
  const keys = parameters.map((p) => p.key);
  let key = "new_input";
  for (let n = 2; isDocInputKeyTaken(key, nodes, keys); n++)
    key = `new_input_${n}`;
  return key;
}

/** Turns the form into the parameter the Inputs panel would save, or says what is wrong. */
export function parseDocInputForm(
  form: DocInputForm,
  oldKey: string | null,
  parameters: ParametersState,
  nodes: Node[],
): { parameter: WorkflowParameter } | { error: string } {
  const key = form.key.trim();
  if (!key) return { error: "Give the input a name." };
  const invalid = validateParameterKey(key);
  if (invalid) return { error: invalid };
  const others = parameters.map((p) => p.key).filter((k) => k !== oldKey);
  const nameProblem = docInputNameProblem(key, oldKey, nodes, others);
  if (nameProblem) return { error: nameProblem };

  const raw = form.defaultText;
  const text = raw.trim();
  // Text keeps its spaces, as the Inputs panel saves it.
  // An empty-string default that was already saved stays as it is when the form doesn't touch it.
  const existing = parameters.find((p) => p.key === oldKey);
  const keepsEmptyDefault =
    raw === "" &&
    existing?.parameterType === "workflow" &&
    existing.defaultValue === "" &&
    existing.dataType === form.dataType;
  let defaultValue: unknown = keepsEmptyDefault ? "" : text === "" ? null : raw;
  if (text !== "")
    switch (form.dataType) {
      case WorkflowParameterValueType.JSON:
        try {
          defaultValue = JSON.parse(text);
        } catch (e) {
          return { error: getInvalidJsonMessage(text, e) };
        }
        break;
      case WorkflowParameterValueType.Integer:
        if (!/^-?\d+$/.test(text)) return { error: "Use a whole number." };
        defaultValue = text;
        break;
      case WorkflowParameterValueType.Float:
        // What Python's float() reads; Number() would also take hex.
        if (!/^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(text))
          return { error: "Use a number." };
        defaultValue = text;
        break;
      case WorkflowParameterValueType.Boolean:
        if (!/^(true|false)$/i.test(text))
          return { error: "Use true or false." };
        defaultValue = text.toLowerCase();
        break;
    }
  return {
    parameter: {
      key,
      parameterType: "workflow",
      dataType: form.dataType,
      description: form.description.trim() || null,
      defaultValue,
    },
  };
}

/** Adds the input, or replaces `oldKey`; a rename rewrites the steps' references to it. */
export function saveDocInput(
  parameters: ParametersState,
  oldKey: string | null,
  parameter: ParametersState[number],
): {
  parameters: ParametersState;
  nodes: (<T extends Node>(nodes: T[]) => T[]) | null;
} {
  if (oldKey === null)
    return { parameters: [...parameters, parameter], nodes: null };
  return {
    parameters: replaceParameter(parameters, oldKey, parameter).map((entry) =>
      entry.parameterType === "context" && entry.sourceParameterKey === oldKey
        ? { ...entry, sourceParameterKey: parameter.key }
        : entry,
    ),
    nodes:
      oldKey === parameter.key
        ? null
        : (nodes) =>
            followRenamedInput(
              renameParameterInNodes(nodes, oldKey, parameter.key),
              oldKey,
              parameter.key,
            ),
  };
}

/**
 * Removes the input. Its `{{ key }}` mentions stay in the text so the Doc shows them as missing;
 * only the structural `parameterKeys` and loop-value links are dropped, which the save would reject.
 */
export function removeDocInput(
  parameters: ParametersState,
  key: string,
):
  | {
      parameters: ParametersState;
      nodes: <T extends Node>(nodes: T[]) => T[];
    }
  | { refused: string } {
  const fed = parameters.filter(
    (p) => p.parameterType === "context" && p.sourceParameterKey === key,
  );
  if (fed.length)
    return {
      refused: `The input ${fed.map((p) => p.key).join(", ")} takes its value from ${key}. Change that input first.`,
    };
  return {
    parameters: parameters.filter((p) => p.key !== key),
    nodes: (nodes) =>
      clearDerivedLoopSources(removeKeyFromNodesParameterKeys(nodes, key), key),
  };
}

// removeKeyFromNodesParameterKeys clears a loop source only when it equals the key; `key.items` and `key["items"]` go too.
function clearDerivedLoopSources<T extends Node>(nodes: T[], key: string): T[] {
  const rooted = (value: unknown) => {
    if (typeof value !== "string") return false;
    const inner = value.trim().replace(/^\{\{\s*|\s*\}\}$/g, "");
    return (
      inner === key ||
      inner.startsWith(`${key}.`) ||
      inner.startsWith(`${key}[`)
    );
  };
  return nodes.map((node) => {
    const data = node.data as Record<string, unknown>;
    if (node.type !== "loop") return node;
    const next = { ...data };
    if (rooted(data.loopValue)) next.loopValue = "";
    if (rooted(data.loopVariableReference)) next.loopVariableReference = "";
    return { ...node, data: next };
  });
}

// The shared rename covers `{{ }}` text and every step's `parameterKeys`; a loop's source holds a bare key the save also checks.
function followRenamedInput<T extends Node>(
  nodes: T[],
  oldKey: string,
  newKey: string,
): T[] {
  const follow = (value: unknown) =>
    typeof value === "string" &&
    (value === oldKey ||
      value.startsWith(`${oldKey}.`) ||
      value.startsWith(`${oldKey}[`))
      ? newKey + value.slice(oldKey.length)
      : value;
  return nodes.map((node) => {
    const data = node.data as Record<string, unknown>;
    if (node.type === "loop")
      return {
        ...node,
        data: {
          ...data,
          loopValue: follow(data.loopValue),
          loopVariableReference: follow(data.loopVariableReference),
        },
      };
    return node;
  });
}
