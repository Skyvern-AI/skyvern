import type { Node } from "@xyflow/react";
import {
  WorkflowParameterValueType,
  type WorkflowBlock,
  type WorkflowBlockType,
} from "../../types/workflowTypes";
import { diffBlockFields } from "../../util/compareBlocks";
import {
  isWorkflowBlockNode,
  type AppNode,
  type WorkflowBlockNode,
} from "../nodes";
import { RunEngine } from "@/api/types";
import {
  parameterIsAzureVaultCredential,
  parameterIsBitwardenCredential,
  parameterIsOnePasswordCredential,
  parameterIsSkyvernCredential,
  type ParametersState,
} from "../types";
import { humanizeKey, reviewFieldValue } from "../panels/workflowReviewDiff";
import { createNode, getWorkflowBlocks } from "../workflowEditorUtils";
import { docDetailsText } from "./docDetailsText";
import type { DocItem } from "./buildDocTree";
import { DOC_FIELD_MAP, type DocFieldMeta } from "./docFieldMap";
import { getDocFilledProseFields, getDocProseFields } from "./docStepRegistry";
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

export type DocSettingRow = {
  key: string;
  label: string;
  value: string;
  // A key setting with no value, shown as its empty text.
  unset?: boolean;
  // A key setting whose saved value differs from a new step's.
  changed?: boolean;
  meta?: DocFieldMeta;
  addLabel?: string;
};

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
export function docStepLineKeys(
  node: AppNode,
  editable: boolean,
  metaKeys: readonly string[] = [],
): string[] {
  const fields = getDocProseFields(node);
  if (!fields.length) return ["description", ...metaKeys];
  const filled = getDocFilledProseFields(node);
  // While editing, the first field is the step's text even when empty; read-only, the first filled one is.
  const main = editable ? fields[0]! : (filled[0] ?? fields[0]!);
  const mainEmpty = !filled.some((f) => f.key === main.key);
  return [
    ...[main.key, ...(mainEmpty ? filled.map((f) => f.key) : [])].map(yamlKey),
    ...metaKeys,
  ];
}

/** A step's key settings split into its summary line (each set one and, where it gets a dashed chip, an unset one; at
 * most two) and Details. An unset first-page field on a step that inherits the open page is in neither, unless a
 * proposal changed it. */
export function docSettingRowsSplit(
  rows: readonly DocSettingRow[],
  editable: boolean,
  opensFirstPage: boolean,
  proposed: (key: string) => boolean = () => false,
): { line: DocSettingRow[]; details: DocSettingRow[] } {
  const shown = rows.filter(
    ({ key, meta, unset }) =>
      !(unset && meta?.emptyChip === "firstPage" && !opensFirstPage) ||
      proposed(key),
  );
  const line = shown
    .filter(
      ({ meta, unset }) =>
        meta &&
        (!unset ||
          (editable &&
            (meta.emptyChip === "always" || meta.emptyChip === "firstPage"))),
    )
    .slice(0, 2);
  return { line, details: shown.filter((row) => !line.includes(row)) };
}

// No `proposed` here: its callers read it for fix lines, and the editor has none while a proposal is shown.
export function docMetaLineKeys(
  node: AppNode,
  loginCredentialKeys: ReadonlySet<string>,
  editable: boolean,
  opensFirstPage: boolean,
): string[] {
  return docSettingRowsSplit(
    docKeySettings(node, loginCredentialKeys),
    editable,
    opensFirstPage,
  ).line.map((row) => row.key);
}

/** The ids of the steps whose lines can be edited where the Doc shows them, as DocScope decides it for each step. */
export function docEditableStepIds(
  items: readonly DocItem[],
  byId: ReadonlyMap<string, AppNode>,
  editable: boolean,
): Set<string> {
  const ids = new Set<string>();
  const visit = (list: readonly DocItem[], readOnly: boolean) => {
    for (const item of list) {
      if (item.kind === "note" || item.kind === "unplaced") continue;
      const node = byId.get(item.nodeId);
      const inner = readOnly || Boolean(item.readOnlyReason);
      if (!inner && node && isWorkflowBlockNode(node) && node.data.editable)
        ids.add(item.nodeId);
      if (item.kind === "loop") visit(item.children, inner);
      if (item.kind === "conditional")
        for (const branch of item.branches) visit(branch.children, inner);
    }
  };
  if (editable) visit(items, false);
  return ids;
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

// Keyed like the custom-settings cache, so a step is serialized once per edit for both lists.
const savedBlocks = new WeakMap<object, Record<string, unknown> | null>();

// The step as it saves; null where the editor cannot serialize it.
function savedBlock(node: WorkflowBlockNode): Record<string, unknown> | null {
  if (!savedBlocks.has(node.data)) {
    let saved: Record<string, unknown> | null = null;
    try {
      const [block] = getWorkflowBlocks([{ ...node, parentId: undefined }], []);
      saved = (block as Record<string, unknown>) ?? null;
    } catch (error) {
      if (import.meta.env.DEV && !warned) {
        warned = true;
        console.warn("Doc: a step could not be serialized for Details", error);
      }
    }
    savedBlocks.set(node.data, saved);
  }
  return savedBlocks.get(node.data) ?? null;
}

const proseKeys = (node: Node) =>
  new Set(getDocProseFields(node).map((f) => yamlKey(f.key)));

// By the saved type: a navigation on the 2.0 engine saves as task_v2, which has its own fields.
const typeSpecs = (saved: Record<string, unknown>) =>
  DOC_FIELD_MAP[saved.block_type as WorkflowBlockType] ?? [];

const shortValue = (value: string) =>
  value.length > MAX_VALUE ? `${value.slice(0, MAX_VALUE - 1)}…` : value;

/** The parameters a login can use as its credential, by the Login panel's own credential rules. */
export function docLoginCredentialKeys(
  parameters: ParametersState,
): ReadonlySet<string> {
  return new Set(
    parameters.flatMap((p) => {
      if (p.parameterType === "workflow")
        return p.dataType === WorkflowParameterValueType.CredentialId
          ? [p.key]
          : [];
      if (p.parameterType !== "credential" && p.parameterType !== "onepassword")
        return [];
      return parameterIsSkyvernCredential(p) ||
        parameterIsBitwardenCredential(p) ||
        parameterIsOnePasswordCredential(p) ||
        parameterIsAzureVaultCredential(p)
        ? [p.key]
        : [];
    }),
  );
}

/** The step type's key settings with their values, except a field the step shows as one of its lines. */
export function docKeySettings(
  node: AppNode,
  loginCredentialKeys: ReadonlySet<string>,
): DocSettingRow[] {
  if (!isWorkflowBlockNode(node)) return [];
  const saved = savedBlock(node);
  if (!saved) return [];
  const lines = proseKeys(node);
  const changedKeys = changedFromNewStep(node, saved);
  return typeSpecs(saved).flatMap((spec) => {
    if (spec.hiddenWhen?.(saved)) return [];
    if (spec.keys.every((key) => lines.has(key))) return [];
    // The Login panel reads only the first entry as the credential and keeps later ones as inputs; a first entry that
    // is not a credential (a dangling reference, a plain input) is left out, since the Doc cannot tell what it is.
    const values = spec.keys.map((key) => {
      const raw = saved[key];
      return spec.credentialsOnly && Array.isArray(raw)
        ? { raw, kept: raw.slice(0, 1) }
        : { raw, kept: raw };
    });
    if (
      spec.credentialsOnly &&
      values.some(
        ({ kept }) =>
          Array.isArray(kept) &&
          kept.some((k) => !loginCredentialKeys.has(String(k))),
      )
    )
      return [];
    const shownByEditor = spec.editorValue?.(
      node.data as Record<string, unknown>,
    );
    const value =
      (typeof shownByEditor === "string" && shownByEditor.trim()
        ? shownByEditor
        : null) ??
      values
        .map(({ kept }, i) => reviewFieldValue(spec.keys[i]!, kept))
        // A value that formats to blank text, such as a list of empty recipients, reads as unset.
        .find((v) => v !== null && v.trim() !== "") ??
      null;
    return [
      {
        key: spec.keys[0]!,
        label: spec.label,
        value: value === null ? spec.empty : shortValue(value),
        unset: value === null,
        meta: spec.meta,
        addLabel: spec.addLabel,
        changed:
          value !== null &&
          (changedKeys?.some((key) => spec.keys.includes(key)) ?? true),
      },
    ];
  });
}

// The saved keys that differ from a new step of the same type; null when the new step cannot be serialized.
function changedFromNewStep(
  node: WorkflowBlockNode,
  saved: Record<string, unknown>,
): string[] | null {
  let base: Record<string, unknown> | null;
  try {
    base = baselineBlock(node);
  } catch {
    return null;
  }
  if (!base) return null;
  return diffBlockFields(
    withoutShownCondition(base) as WorkflowBlock,
    withoutShownCondition(saved) as WorkflowBlock,
  ).map(({ key }) => key);
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
  const block = savedBlock(node);
  let base: Record<string, unknown> | null;
  try {
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
    ...proseKeys(node),
    // A key a hiddenWhen rule leaves out has no row, so a non-default value of it shows here.
    ...typeSpecs(block)
      .filter((spec) => !spec.hiddenWhen?.(block))
      .flatMap((spec) => spec.keys),
  ]);
  const saved = block;
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
    return [{ key, label: humanizeKey(key), value: shortValue(value) }];
  });
}
