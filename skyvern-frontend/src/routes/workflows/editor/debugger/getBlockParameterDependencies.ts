import type {
  ContextParameter,
  Parameter,
  WorkflowBlock,
  WorkflowParameter,
} from "@/routes/workflows/types/workflowTypes";

const JINJA_VARIABLE_REGEX = /\{\{\s*([^}|]+?)\s*(?:\|[^}]*)?\}\}/g;
const SIMPLE_JINJA_PATH_REGEX =
  /^[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*$/;
const JINJA_IDENTIFIER_REGEX = /[a-zA-Z_][a-zA-Z0-9_]*/g;

const MAX_RECURSION_DEPTH = 30;

function extractJinjaRootsFromString(
  text: string,
  workflowParamKeys: Set<string>,
): string[] {
  const roots = new Set<string>();
  JINJA_VARIABLE_REGEX.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = JINJA_VARIABLE_REGEX.exec(text)) !== null) {
    const inner = match[1]?.trim();
    if (!inner) {
      continue;
    }

    if (SIMPLE_JINJA_PATH_REGEX.test(inner)) {
      const root = inner.split(".")[0]?.trim();
      if (root) {
        roots.add(root);
      }
      continue;
    }

    JINJA_IDENTIFIER_REGEX.lastIndex = 0;
    let identifierMatch: RegExpExecArray | null;
    while ((identifierMatch = JINJA_IDENTIFIER_REGEX.exec(inner)) !== null) {
      const identifier = identifierMatch[0];
      if (workflowParamKeys.has(identifier)) {
        roots.add(identifier);
      }
    }
  }
  return [...roots];
}

function collectTemplateStringsFromValue(
  value: unknown,
  depth: number,
  skipLoopBodies: boolean,
): string[] {
  if (depth > MAX_RECURSION_DEPTH || value === null || value === undefined) {
    return [];
  }

  if (typeof value === "string") {
    return [value];
  }

  if (Array.isArray(value)) {
    return value.flatMap((item) =>
      collectTemplateStringsFromValue(item, depth + 1, skipLoopBodies),
    );
  }

  if (typeof value !== "object") {
    return [];
  }

  const record = value as Record<string, unknown>;
  const isLoopContainer =
    record.block_type === "for_loop" || record.block_type === "while_loop";

  const strings: string[] = [];
  for (const [key, child] of Object.entries(record)) {
    if (key === "output_parameter") {
      continue;
    }
    if (key === "parameters") {
      continue;
    }
    if (skipLoopBodies && isLoopContainer && key === "loop_blocks") {
      continue;
    }
    strings.push(
      ...collectTemplateStringsFromValue(child, depth + 1, skipLoopBodies),
    );
  }
  return strings;
}

function collectReferencedKeysFromJinja(
  block: WorkflowBlock,
  workflowParamKeys: Set<string>,
): Set<string> {
  const keys = new Set<string>();
  for (const text of collectTemplateStringsFromValue(block, 0, true)) {
    for (const root of extractJinjaRootsFromString(text, workflowParamKeys)) {
      keys.add(root);
    }
  }
  return keys;
}

function readExplicitParameterKeys(block: WorkflowBlock): string[] {
  const raw = (block as { parameter_keys?: unknown }).parameter_keys;
  if (!Array.isArray(raw)) {
    return [];
  }
  return raw.filter((k): k is string => typeof k === "string" && k.length > 0);
}

function resolveWorkflowParameterKey(param: Parameter): string | null {
  if (param.parameter_type === "workflow") {
    return param.key;
  }
  if (param.parameter_type === "context") {
    return resolveWorkflowParameterKey(
      (param as ContextParameter).source as Parameter,
    );
  }
  return null;
}

function readParameterKeysFromBlockParameters(block: WorkflowBlock): string[] {
  if (!("parameters" in block) || !Array.isArray(block.parameters)) {
    return [];
  }
  const keys: string[] = [];
  for (const p of block.parameters) {
    const workflowKey = resolveWorkflowParameterKey(p);
    if (workflowKey) {
      keys.push(workflowKey);
    }
  }
  return keys;
}

function collectLoopAndBranchKeys(
  block: WorkflowBlock,
  workflowParamKeys: Set<string>,
): string[] {
  const keys: string[] = [];

  if (block.block_type === "for_loop" && block.loop_over?.key) {
    const loopKey = resolveWorkflowParameterKey(block.loop_over);
    if (loopKey) {
      keys.push(loopKey);
    }
  }

  if (block.block_type === "while_loop" && block.condition?.expression) {
    keys.push(
      ...extractJinjaRootsFromString(
        block.condition.expression,
        workflowParamKeys,
      ),
    );
  }

  if (block.block_type === "conditional") {
    for (const branch of block.branch_conditions) {
      if (branch.criteria?.expression) {
        keys.push(
          ...extractJinjaRootsFromString(
            branch.criteria.expression,
            workflowParamKeys,
          ),
        );
      }
    }
  }

  return keys;
}

function collectDependencyKeySet(
  block: WorkflowBlock,
  workflowParamKeys: Set<string>,
): Set<string> {
  const keys = new Set<string>();

  for (const k of readExplicitParameterKeys(block)) {
    keys.add(k);
  }
  for (const k of readParameterKeysFromBlockParameters(block)) {
    keys.add(k);
  }
  for (const k of collectReferencedKeysFromJinja(block, workflowParamKeys)) {
    keys.add(k);
  }
  for (const k of collectLoopAndBranchKeys(block, workflowParamKeys)) {
    keys.add(k);
  }

  return keys;
}

export function getBlockParameterDependencies(
  block: WorkflowBlock | undefined,
  workflowParameters: WorkflowParameter[],
): WorkflowParameter[] {
  if (!block || workflowParameters.length === 0) {
    return workflowParameters;
  }

  const workflowParamKeys = new Set(workflowParameters.map((p) => p.key));
  const wanted = collectDependencyKeySet(block, workflowParamKeys);

  return workflowParameters.filter((p) => wanted.has(p.key));
}
