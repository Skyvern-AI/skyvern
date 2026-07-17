import { parse } from "yaml";

import type {
  WorkflowSettings,
  WorkflowParameter,
} from "@/routes/workflows/types/workflowTypes";
import type {
  BlockYAML,
  ParameterYAML,
} from "@/routes/workflows/types/workflowYamlTypes";
import type { WorkflowSaveData } from "@/store/WorkflowHasChangesStore";
import {
  isWorkflowYamlDirty,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";

// A frozen copy of the editable draft, taken at a clean baseline (see
// WorkflowSnapshotStore). Diffed against the live draft to describe unsaved
// edits. Both sides come from the SAME nodes->YAML serializer, so content the
// canvas materialized post-load (e.g. a login-block credential autofill) is
// present on both sides and never reads as a phantom edit.
export type WorkflowSnapshot = {
  blocks: Array<BlockYAML>;
  parameters: Array<ParameterYAML | WorkflowParameter>;
  settings: WorkflowSettings;
  title: string;
};

// Recursively sort object keys and drop empty values (null / "" / [] / {}) so
// two serializations of the same content compare equal despite cosmetic
// key-order or empty-value differences.
function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) {
    const items = value.map(canonicalize).filter((item) => item !== undefined);
    return items.length > 0 ? items : undefined;
  }
  if (value !== null && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(value as Record<string, unknown>).sort()) {
      const canon = canonicalize((value as Record<string, unknown>)[key]);
      if (canon !== undefined) {
        out[key] = canon;
      }
    }
    return Object.keys(out).length > 0 ? out : undefined;
  }
  if (value === null || value === "") {
    return undefined;
  }
  return value;
}

function canonicalKey(value: unknown): string {
  return JSON.stringify(canonicalize(value));
}

type LoopBlock = BlockYAML & { loop_blocks?: Array<BlockYAML> };

// A block's own content, excluding its label (so a rename can be matched) and
// its loop_blocks (children are diffed separately, so a child edit flags only
// the child, not its container).
function ownFingerprint(block: BlockYAML): string {
  const { label, loop_blocks, ...self } = block as LoopBlock;
  void label;
  void loop_blocks;
  return canonicalKey(self);
}

// A block's whole subtree excluding only its label — used to pair a removed
// block with an added one as a rename only when everything but the label matches.
function subtreeFingerprint(block: BlockYAML): string {
  const { label, ...self } = block as LoopBlock;
  void label;
  return canonicalKey(self);
}

function loopChildren(block: BlockYAML): Array<BlockYAML> {
  const children = (block as LoopBlock).loop_blocks;
  return Array.isArray(children) ? children : [];
}

function blockNoun(blockType: string): string {
  return blockType.split("_").join(" ");
}

// Diff one nesting level, recursing into matched/renamed loop containers. Labels
// are unique per level (not globally), so each level is keyed by label and
// renames are paired by identical subtree content. Recursing per level keeps a
// container rename from flagging its unchanged children — a child's identity
// never encodes an ancestor's label.
export function summarizeBlockChanges(
  baseline: Array<BlockYAML>,
  draft: Array<BlockYAML>,
): Array<string> {
  const baseByLabel = new Map(baseline.map((b) => [b.label, b] as const));
  const draftByLabel = new Map(draft.map((b) => [b.label, b] as const));
  const edited: Array<string> = [];
  const childChanges: Array<string> = [];
  const addedBlocks: Array<BlockYAML> = [];
  const removedBlocks: Array<BlockYAML> = [];

  for (const draftBlock of draft) {
    const baseBlock = baseByLabel.get(draftBlock.label);
    if (!baseBlock) {
      addedBlocks.push(draftBlock);
      continue;
    }
    if (ownFingerprint(baseBlock) !== ownFingerprint(draftBlock)) {
      edited.push(`Edited block "${draftBlock.label}"`);
    }
    childChanges.push(
      ...summarizeBlockChanges(
        loopChildren(baseBlock),
        loopChildren(draftBlock),
      ),
    );
  }
  for (const baseBlock of baseline) {
    if (!draftByLabel.has(baseBlock.label)) {
      removedBlocks.push(baseBlock);
    }
  }

  // A removed block whose whole subtree (label aside) matches an added one is a
  // rename; when the content also changed, the subtrees differ and it stays a
  // remove + add.
  const renamed: Array<string> = [];
  const takenAdded = new Set<number>();
  const removedLines: Array<string> = [];
  for (const rem of removedBlocks) {
    const matchIdx = addedBlocks.findIndex(
      (a, idx) =>
        !takenAdded.has(idx) &&
        subtreeFingerprint(a) === subtreeFingerprint(rem),
    );
    const match = matchIdx >= 0 ? addedBlocks[matchIdx] : undefined;
    if (match) {
      takenAdded.add(matchIdx);
      renamed.push(`Renamed block "${rem.label}" to "${match.label}"`);
    } else {
      removedLines.push(`Removed block "${rem.label}"`);
    }
  }
  const addedLines = addedBlocks
    .filter((_, idx) => !takenAdded.has(idx))
    .map((a) => `Added ${blockNoun(a.block_type)} block "${a.label}"`);

  return [
    ...addedLines,
    ...renamed,
    ...edited,
    ...childChanges,
    ...removedLines,
  ];
}

// Only workflow-type parameters are user-managed; aws_secret / echo / email
// secrets are synthesized at save time and would read as phantom changes.
type ParameterLike = { key: string; parameter_type: string };

function summarizeParameterChanges(
  baseline: Array<ParameterLike>,
  draft: Array<ParameterLike>,
): Array<string> {
  const isWorkflowParam = (p: ParameterLike) => p.parameter_type === "workflow";
  const baseMap = new Map(
    baseline.filter(isWorkflowParam).map((p) => [p.key, p] as const),
  );
  const draftMap = new Map(
    draft.filter(isWorkflowParam).map((p) => [p.key, p] as const),
  );
  const changes: Array<string> = [];
  // Match by key and fingerprint the whole parameter (default value,
  // description, type…) so any field edit reads as one "Edited parameter" line,
  // mirroring the block grammar. One entry per key even if several fields change.
  for (const [key, param] of draftMap) {
    const base = baseMap.get(key);
    if (!base) {
      changes.push(`Added parameter "${key}"`);
    } else if (canonicalKey(param) !== canonicalKey(base)) {
      changes.push(`Edited parameter "${key}"`);
    }
  }
  for (const key of baseMap.keys()) {
    if (!draftMap.has(key)) {
      changes.push(`Removed parameter "${key}"`);
    }
  }
  return changes;
}

// Every user-editable workflow setting maps to a specific, value-free line so a
// settings change is named rather than falling to the generic bucket. Grouped
// pairs (browser profile, sequential runs) read as one line. TOTP and other
// workflow-object fields are not diffed here — they live outside the snapshot.
const SETTINGS_LABELS: Array<{
  fields: Array<keyof WorkflowSettings>;
  label: string;
}> = [
  { fields: ["proxyLocation"], label: "Changed proxy location" },
  { fields: ["webhookCallbackUrl"], label: "Changed webhook callback URL" },
  {
    fields: ["persistBrowserSession"],
    label: "Toggled persist browser session",
  },
  { fields: ["pinSavedSessionIp"], label: "Toggled pinned session IP" },
  {
    fields: ["browserProfileId", "browserProfileKey"],
    label: "Changed browser profile",
  },
  { fields: ["model"], label: "Changed model" },
  { fields: ["maxScreenshotScrolls"], label: "Changed max screenshot scrolls" },
  { fields: ["maxElapsedTimeMinutes"], label: "Changed max run time" },
  { fields: ["extraHttpHeaders"], label: "Changed extra HTTP headers" },
  { fields: ["cdpConnectHeaders"], label: "Changed CDP connect headers" },
  { fields: ["runWith"], label: "Changed run mode" },
  { fields: ["codeVersion"], label: "Changed code version" },
  { fields: ["scriptCacheKey"], label: "Changed script cache key" },
  { fields: ["aiFallback"], label: "Toggled AI fallback" },
  { fields: ["enableSelfHealing"], label: "Toggled self-healing" },
  {
    fields: ["runSequentially", "sequentialKey"],
    label: "Changed sequential run settings",
  },
  { fields: ["finallyBlockLabel"], label: "Changed finally block" },
  { fields: ["workflowSystemPrompt"], label: "Changed workflow system prompt" },
  { fields: ["errorCodeMapping"], label: "Changed error handling" },
];

function summarizeSettingsChanges(
  baseline: WorkflowSettings | null,
  draft: WorkflowSettings | null,
): Array<string> {
  if (!baseline || !draft) {
    return [];
  }
  const changes: Array<string> = [];
  for (const { fields, label } of SETTINGS_LABELS) {
    const changed = fields.some(
      (field) => canonicalKey(baseline[field]) !== canonicalKey(draft[field]),
    );
    if (changed) {
      changes.push(label);
    }
  }
  return changes;
}

// While the Code/YAML editor is open with uncommitted edits, the canvas graph
// (saveData) lags the draft, and the actual save commits the YAML draft. Parse
// the YAML draft so YAML-only edits are reflected. Malformed YAML mid-typing
// falls back to the canvas.
function effectiveDraft(saveData: WorkflowSaveData): WorkflowSnapshot {
  let blocks = saveData.blocks;
  let parameters: WorkflowSnapshot["parameters"] = saveData.parameters;
  const yamlState = useWorkflowYamlEditorStore.getState();
  if (yamlState.active && isWorkflowYamlDirty(yamlState)) {
    try {
      const parsed = parse(yamlState.draft) as {
        blocks?: Array<BlockYAML>;
        parameters?: Array<ParameterYAML>;
      } | null;
      if (parsed && Array.isArray(parsed.blocks)) {
        blocks = parsed.blocks;
        if (Array.isArray(parsed.parameters)) {
          parameters = parsed.parameters;
        }
      }
    } catch {
      // fall through to the canvas draft
    }
  }
  // Only user-editable content is diffed. workflowDefinitionVersion (bumped by
  // the save-time v2 upgrade) and other workflow-object fields are excluded so
  // non-user churn can never surface as a change.
  return {
    blocks,
    parameters,
    settings: saveData.settings,
    title: saveData.title,
  };
}

// The clean-baseline snapshot, frozen at the first user edit after a load or
// save (see WorkflowSnapshotStore). Same extraction as the diff, so a fresh
// load compares equal.
export function snapshotOf(saveData: WorkflowSaveData): WorkflowSnapshot {
  return effectiveDraft(saveData);
}

export function isDraftDirty(
  saveData: WorkflowSaveData,
  snapshot: WorkflowSnapshot | null,
): boolean {
  if (!snapshot) {
    return false;
  }
  return canonicalKey(effectiveDraft(saveData)) !== canonicalKey(snapshot);
}

/**
 * Human-readable summary of the unsaved edits between the live draft and the
 * clean-baseline `snapshot`. Returns [] when there is no snapshot yet (no user
 * edit since the last load/save) or the draft matches it, so a freshly loaded
 * workflow — even one the canvas auto-materialized on open — lists nothing.
 * Covers the workflow rename, block add/edit/remove/rename, workflow-parameter
 * add/edit/remove, and every workflow setting. A pure block reorder reads as an
 * "Edited block" (its linkage fields changed) — a deliberate ceiling. The
 * generic "Other workflow changes" line is a catch-all for anything no specific
 * summarizer covers (a future schema field, or a non-workflow-parameter edit
 * that isn't already reflected as a block edit).
 */
export function summarizeWorkflowChanges(
  saveData: WorkflowSaveData,
  snapshot: WorkflowSnapshot | null,
): Array<string> {
  if (!snapshot) {
    return [];
  }
  const draft = effectiveDraft(saveData);
  const changes: Array<string> = [];
  if (draft.title !== snapshot.title) {
    changes.push(`Renamed workflow to "${draft.title}"`);
  }
  changes.push(...summarizeBlockChanges(snapshot.blocks, draft.blocks));
  changes.push(
    ...summarizeParameterChanges(
      snapshot.parameters as Array<ParameterLike>,
      draft.parameters as Array<ParameterLike>,
    ),
  );
  changes.push(...summarizeSettingsChanges(snapshot.settings, draft.settings));

  if (changes.length === 0 && isDraftDirty(saveData, snapshot)) {
    changes.push("Other workflow changes");
  }
  return changes;
}
