import type { WorkflowBlock } from "../../types/workflowTypes";
import { diffBlockFields } from "../../util/compareBlocks";
import {
  isWorkflowBlockNode,
  type AppNode,
  type WorkflowBlockNode,
} from "../nodes";
import { RunEngine } from "@/api/types";
import { humanizeKey, reviewFieldValue } from "../panels/workflowReviewDiff";
import { createNode, getWorkflowBlocks } from "../workflowEditorUtils";
import { docDetailsText } from "./docDetailsText";
import {
  getDocFilledProseFields,
  getDocKeyFields,
  getDocProseFields,
} from "./docStepRegistry";
import { taskv2NodeDefaultData } from "../nodes/Taskv2Node/types";

// createNode makes a navigation step for taskv2, which left the add menu, but saved task_v2 blocks still load as taskv2.
const newStepData = (node: WorkflowBlockNode): Record<string, unknown> =>
  node.type === "taskv2"
    ? taskv2NodeDefaultData
    : (createNode({ id: node.id }, node.type, "").data as Record<
        string,
        unknown
      >);

export const customSettingsText = (count: number) =>
  `${count} custom setting${count === 1 ? "" : "s"}`;

export const customSettingsShortText = (count: number) => `${count} custom`;

export type DocSettingRow = { key: string; label: string; value: string };

// Saved keys a step shows outside Details (its title, list, branches, wait, notes) or that only the editor writes.
export const DOC_SHOWN_ON_STEP: ReadonlySet<string> = new Set([
  "label",
  "title",
  "block_type",
  "branch_conditions",
  // A for-each loop's list as the API names it; the editor saves it as the two keys below.
  "loop_over",
  "loop_variable_reference",
  "loop_over_parameter_key",
  "wait_sec",
  "code",
  "steps",
  "user_owned_goal",
  "goal_needs_regeneration",
  "code_edited_by_hand",
  "notes",
  "trailing_notes",
]);

// Saved keys the Doc shows in its own place (a line, Inputs, Outputs, the step itself) or that only the editor writes.
const SHOWN_ELSEWHERE = new Set<string>([
  ...DOC_SHOWN_ON_STEP,
  "description",
  "parameter_keys",
  "parameters",
  "data_schema",
  "json_schema",
]);

export const yamlKey = (key: string) =>
  key.replace(/([a-z0-9])([A-Z])/g, "$1_$2").toLowerCase();

const MAX_VALUE = 200;

/** The saved keys of the lines a step shows with Details closed, as the step renders them when no line is being edited. */
export function docStepLineKeys(node: AppNode, editable: boolean): string[] {
  const fields = getDocProseFields(node);
  if (!fields.length) return ["description"];
  const filled = getDocFilledProseFields(node);
  // While editing, the first field is the step's text even when empty; read-only, the first filled one is.
  const main = editable ? fields[0]! : (filled[0] ?? fields[0]!);
  const mainEmpty = !filled.some((f) => f.key === main.key);
  return [main.key, ...(mainEmpty ? filled.map((f) => f.key) : [])].map(
    yamlKey,
  );
}

/** Whether a saved key shows on the step with Details closed: its lines, title, list, branches, wait or notes. */
export function docStepShowsKey(
  node: AppNode,
  key: string,
  lineKeys: readonly string[],
): boolean {
  return (
    key.startsWith("__") ||
    DOC_SHOWN_ON_STEP.has(key) ||
    lineKeys.includes(key) ||
    // A while loop's expression is its line; the rest of its condition is a custom setting.
    (key === "condition" &&
      node.type === "loop" &&
      !docCustomSettings(node)?.some((row) => row.key === key))
  );
}

// The fields that pick the saved block type or its serialized branch: a new step keeps them.
function serializedTypeFields(
  node: WorkflowBlockNode,
): Record<string, unknown> {
  if (node.type === "loop") return { loopKind: node.data.loopKind };
  if (node.type === "navigation" && node.data.engine === RunEngine.SkyvernV2)
    return { engine: node.data.engine };
  if (node.type === "sendEmail") return { transport: node.data.transport };
  if (node.type === "fileDownload")
    return { downloadTarget: node.data.downloadTarget };
  return {};
}

// A branch the saved type does not already name is itself a setting.
const REPORTED_BRANCH_FIELDS = new Set(["transport", "downloadTarget"]);

// Keyed by the node's data: React Flow makes a new node object on a move or a selection, not a new data object.
const cache = new WeakMap<object, DocSettingRow[] | null>();
// A new step's saved form depends only on its type and the fields that pick its saved type.
const baselines = new Map<string, Record<string, unknown> | null>();
let warned = false;

function baselineBlock(
  node: WorkflowBlockNode,
): Record<string, unknown> | null {
  const typeFields = serializedTypeFields(node);
  const key = `${node.type}:${JSON.stringify(typeFields)}`;
  if (!baselines.has(key)) {
    const [base] = getWorkflowBlocks(
      [
        {
          ...node,
          parentId: undefined,
          data: { ...newStepData(node), ...typeFields, label: "" },
        } as AppNode,
      ],
      [],
    );
    baselines.set(key, (base as Record<string, unknown>) ?? null);
  }
  return baselines.get(key) ?? null;
}

// A while loop's expression is its main line; the rest of its condition is a setting.
function withoutShownCondition(block: Record<string, unknown>) {
  const condition = block.condition;
  if (block.block_type !== "while_loop" || !condition) return block;
  const rest = { ...(condition as Record<string, unknown>) };
  delete rest.expression;
  return { ...block, condition: rest };
}

/** Every saved setting of the step that differs from a new step of its saved type, named as the review card names it; null when the step cannot be saved as it stands. */
export function docCustomSettings(node: AppNode): DocSettingRow[] | null {
  if (!isWorkflowBlockNode(node)) return [];
  const cached = cache.get(node.data);
  if (cached !== undefined) return cached;
  const rows = computeCustomSettings(node);
  cache.set(node.data, rows);
  return rows;
}

function computeCustomSettings(
  node: WorkflowBlockNode,
): DocSettingRow[] | null {
  let block: unknown;
  let base: Record<string, unknown> | null;
  try {
    [block] = getWorkflowBlocks([{ ...node, parentId: undefined }], []);
    base = baselineBlock(node);
  } catch (error) {
    if (import.meta.env.DEV && !warned) {
      warned = true;
      console.warn("Doc: a step could not be serialized for Details", error);
    }
    return null;
  }
  if (!block || !base) return null;
  const shown = new Set([
    ...SHOWN_ELSEWHERE,
    ...[...getDocProseFields(node), ...getDocKeyFields(node)].map((f) =>
      yamlKey(f.key),
    ),
  ]);
  // The Agent row names a trigger's target by its title; a loaded or proposed trigger starts without one.
  if (node.type === "workflowTrigger" && node.data.workflowTitle.trim())
    shown.add("workflow_permanent_id");
  const saved = block as Record<string, unknown>;
  const defaultData = newStepData(node);
  const branches = Object.entries(serializedTypeFields(node)).flatMap(
    ([field, value]) =>
      REPORTED_BRANCH_FIELDS.has(field) &&
      value !== defaultData[field] &&
      !shown.has(yamlKey(field))
        ? [{ key: yamlKey(field), after: saved[yamlKey(field)] ?? value }]
        : [],
  );
  return [
    ...branches,
    ...diffBlockFields(
      withoutShownCondition(base) as WorkflowBlock,
      withoutShownCondition(saved) as WorkflowBlock,
    ),
  ].flatMap(({ key, after }) => {
    if (shown.has(key)) return [];
    // A setting the user cleared still differs from its default.
    const value = reviewFieldValue(key, after) ?? docDetailsText.notSet;
    const short =
      value.length > MAX_VALUE ? `${value.slice(0, MAX_VALUE - 1)}…` : value;
    return [{ key, label: humanizeKey(key), value: short }];
  });
}
