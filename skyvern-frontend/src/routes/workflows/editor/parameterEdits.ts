import type { Node } from "@xyflow/react";
import type { ParametersState } from "./types";
import { replaceJinjaReferenceInNodes } from "./workflowEditorUtils";

export const RESERVED_PARAMETER_KEYS = [
  "current_item",
  "current_value",
  "current_index",
  "current_date",
  "workflow_title",
  "workflow_id",
  "workflow_permanent_id",
  "workflow_run_id",
  "workflow_run_outputs",
  "workflow_run_summary",
];

/**
 * Parameter keys are used in Jinja2 templates, so they must be valid identifiers.
 * Returns an error message if invalid, or null if valid.
 */
export function validateParameterKey(key: string): string | null {
  if (!key) return null; // Empty key is handled separately

  if (/\s/.test(key)) {
    return "Key cannot contain whitespace characters. Consider using underscores (_) instead.";
  }

  const validIdentifierRegex = /^[a-zA-Z_][a-zA-Z0-9_]*$/;
  if (!validIdentifierRegex.test(key)) {
    if (/^[0-9]/.test(key)) {
      return "Key cannot start with a digit. Input keys must start with a letter or underscore.";
    }
    if (key.includes("/")) {
      return "Key cannot contain '/' characters. Use underscores instead (e.g., 'State_or_Province' instead of 'State/Province').";
    }
    if (key.includes("-")) {
      return "Key cannot contain '-' characters. Use underscores instead (e.g., 'my_parameter' instead of 'my-parameter').";
    }
    if (key.includes(".")) {
      return "Key cannot contain '.' characters. Use underscores instead.";
    }
    return "Key must be a valid identifier (only letters, digits, and underscores; cannot start with a digit).";
  }

  return null;
}

/** Replaces the parameter saved under `oldKey`; context inputs read from it follow a key change. */
export function replaceParameter(
  parameters: ParametersState,
  oldKey: string,
  edited: ParametersState[number],
): ParametersState {
  return parameters.map((parameter) => {
    if (parameter.key === oldKey) return edited;
    if (
      parameter.parameterType === "context" &&
      parameter.sourceParameterKey === oldKey
    )
      return { ...parameter, sourceParameterKey: edited.key };
    return parameter;
  });
}

/** Every `{{ oldKey }}` and `parameterKeys` entry in the steps follows a renamed input. */
export function renameParameterInNodes<T extends Node>(
  nodes: T[],
  oldKey: string,
  newKey: string,
): T[] {
  if (!oldKey || !newKey || oldKey === newKey) return nodes;
  return replaceJinjaReferenceInNodes(nodes, oldKey, newKey).map((node) => {
    // Any step type that carries parameterKeys follows, not a fixed list of types.
    const parameterKeys = (node.data as { parameterKeys?: unknown })
      .parameterKeys;
    if (!Array.isArray(parameterKeys) || !parameterKeys.includes(oldKey))
      return node;
    return {
      ...node,
      data: {
        ...node.data,
        parameterKeys: parameterKeys.map((key) =>
          key === oldKey ? newKey : key,
        ),
      },
    };
  });
}
