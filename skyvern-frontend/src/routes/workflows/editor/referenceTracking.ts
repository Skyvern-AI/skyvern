import type { Node } from "@xyflow/react";

/**
 * Regex to find Jinja-style variable references: {{ variable_name }}
 * Captures the variable name without the braces
 * Supports dotted references like {{ block_1_output.field }}
 */
const VARIABLE_REFERENCE_REGEX =
  /\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)\s*\}\}/g;

/**
 * Represents a reference found in a node field
 */
export interface VariableReference {
  variableName: string; // e.g., "website_url" or "block_1_output"
  nodeId: string; // ID of the node containing the reference
  nodeLabel: string; // Human-readable node name
  fieldPath: string; // e.g., "url", "navigationGoal", "dataExtractionGoal"
  fullMatch: string; // e.g., "{{ website_url }}"
}

// Generic node type that accepts any data shape
type GenericNode = Node<Record<string, unknown>>;

/**
 * Extract all variable references from a string value
 */
function extractReferences(
  value: string,
  nodeId: string,
  nodeLabel: string,
  fieldPath: string,
): VariableReference[] {
  const refs: VariableReference[] = [];
  let match;

  // Create a new regex instance to avoid issues with lastIndex
  const regex = new RegExp(VARIABLE_REFERENCE_REGEX.source, "g");

  while ((match = regex.exec(value)) !== null) {
    if (match[1]) {
      refs.push({
        variableName: match[1],
        nodeId,
        nodeLabel,
        fieldPath,
        fullMatch: match[0],
      });
    }
  }

  return refs;
}

/**
 * Recursively scan an object for string values containing variable references.
 * Skips the 'parameterKeys' field since those are tracked separately.
 */
function scanObjectForReferences(
  obj: unknown,
  nodeId: string,
  nodeLabel: string,
  currentPath: string = "",
): VariableReference[] {
  const refs: VariableReference[] = [];

  if (typeof obj === "string") {
    refs.push(...extractReferences(obj, nodeId, nodeLabel, currentPath));
  } else if (Array.isArray(obj)) {
    obj.forEach((item, index) => {
      refs.push(
        ...scanObjectForReferences(
          item,
          nodeId,
          nodeLabel,
          `${currentPath}[${index}]`,
        ),
      );
    });
  } else if (obj && typeof obj === "object") {
    Object.entries(obj).forEach(([key, value]) => {
      // Skip fields that are tracked separately or not relevant
      if (key === "parameterKeys" || key === "id" || key === "type") {
        return;
      }
      const newPath = currentPath ? `${currentPath}.${key}` : key;
      refs.push(...scanObjectForReferences(value, nodeId, nodeLabel, newPath));
    });
  }

  return refs;
}

/**
 * Find all inline template references to a specific variable in the given nodes.
 * This searches for {{ variableName }} patterns in string fields.
 */
export function findReferencesToVariable(
  nodes: GenericNode[],
  variableName: string,
): VariableReference[] {
  const allRefs: VariableReference[] = [];

  nodes.forEach((node) => {
    if (!node.data || typeof node.data !== "object") {
      return;
    }
    const nodeLabel =
      "label" in node.data && typeof node.data.label === "string"
        ? node.data.label
        : node.id;
    const refs = scanObjectForReferences(node.data, node.id, nodeLabel, "");
    allRefs.push(...refs.filter((ref) => ref.variableName === variableName));
  });

  return allRefs;
}

/**
 * Find all nodes that have inline template references to a specific variable.
 * Returns a list of nodes with the fields that contain references.
 */
export function findNodesUsingVariable(
  nodes: GenericNode[],
  variableName: string,
): { nodeId: string; nodeLabel: string; fields: string[] }[] {
  const refs = findReferencesToVariable(nodes, variableName);

  // Group by node
  const nodeMap = new Map<
    string,
    { nodeLabel: string; fields: Set<string> }
  >();

  refs.forEach((ref) => {
    const existing = nodeMap.get(ref.nodeId) || {
      nodeLabel: ref.nodeLabel,
      fields: new Set<string>(),
    };
    existing.fields.add(ref.fieldPath);
    nodeMap.set(ref.nodeId, existing);
  });

  return Array.from(nodeMap.entries()).map(([nodeId, data]) => ({
    nodeId,
    nodeLabel: data.nodeLabel,
    fields: Array.from(data.fields),
  }));
}

/**
 * Escape special regex characters in a string
 */
function escapeRegex(string: string): string {
  return string.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Replace all occurrences of a variable name with a new name in a string
 */
function replaceVariableInString(
  value: string,
  oldName: string,
  newName: string,
): string {
  // Match {{ oldName }} with flexible whitespace
  const regex = new RegExp(
    `\\{\\{\\s*${escapeRegex(oldName)}\\s*\\}\\}`,
    "g",
  );
  return value.replace(regex, `{{ ${newName} }}`);
}

/**
 * Recursively replace variable references in an object.
 * Returns a new object (immutable).
 */
function replaceVariableInObject<T>(obj: T, oldName: string, newName: string): T {
  if (typeof obj === "string") {
    return replaceVariableInString(obj, oldName, newName) as T;
  }

  if (Array.isArray(obj)) {
    return obj.map((item) =>
      replaceVariableInObject(item, oldName, newName),
    ) as T;
  }

  if (obj && typeof obj === "object") {
    const result: Record<string, unknown> = {};
    Object.entries(obj).forEach(([key, value]) => {
      result[key] = replaceVariableInObject(value, oldName, newName);
    });
    return result as T;
  }

  return obj;
}

/**
 * Replace all inline template references to a variable across all nodes.
 * Returns a new array of nodes with updated data (immutable).
 *
 * Note: This only handles inline {{ variable }} references in string fields.
 * The parameterKeys array should be updated separately.
 */
export function replaceVariableInNodes<T extends GenericNode>(
  nodes: T[],
  oldName: string,
  newName: string,
): T[] {
  return nodes.map((node) => {
    if (!node.data) {
      return node;
    }
    return {
      ...node,
      data: replaceVariableInObject(node.data, oldName, newName),
    };
  });
}

/**
 * Remove all occurrences of a variable reference from a string.
 * Replaces {{ variableName }} with empty string.
 */
function removeVariableFromString(value: string, variableName: string): string {
  const regex = new RegExp(
    `\\{\\{\\s*${escapeRegex(variableName)}\\s*\\}\\}`,
    "g",
  );
  return value.replace(regex, "");
}

/**
 * Recursively remove variable references from an object.
 * Returns a new object (immutable).
 */
function removeVariableFromObject<T>(obj: T, variableName: string): T {
  if (typeof obj === "string") {
    return removeVariableFromString(obj, variableName) as T;
  }

  if (Array.isArray(obj)) {
    return obj.map((item) => removeVariableFromObject(item, variableName)) as T;
  }

  if (obj && typeof obj === "object") {
    const result: Record<string, unknown> = {};
    Object.entries(obj).forEach(([key, value]) => {
      result[key] = removeVariableFromObject(value, variableName);
    });
    return result as T;
  }

  return obj;
}

/**
 * Remove all inline template references to a variable across all nodes.
 * Returns a new array of nodes with updated data (immutable).
 *
 * Note: This only handles inline {{ variable }} references in string fields.
 * The parameterKeys array should be updated separately.
 */
export function removeVariableFromNodes<T extends GenericNode>(
  nodes: T[],
  variableName: string,
): T[] {
  return nodes.map((node) => {
    if (!node.data) {
      return node;
    }
    return {
      ...node,
      data: removeVariableFromObject(node.data, variableName),
    };
  });
}

/**
 * Check if any nodes have inline template references to a specific variable.
 */
export function hasReferencesToVariable(
  nodes: GenericNode[],
  variableName: string,
): boolean {
  return findReferencesToVariable(nodes, variableName).length > 0;
}
