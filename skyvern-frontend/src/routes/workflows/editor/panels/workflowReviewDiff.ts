import { stableStringify } from "@/util/stableStringify";

import type { WorkflowVersion } from "../../hooks/useWorkflowVersionsQuery";
import {
  isNestedLoopWorkflowBlock,
  type Parameter,
  type WorkflowBlock,
  type WorkflowSettings,
} from "../../types/workflowTypes";
import {
  areValuesEquivalent,
  diffBlockFields,
  diffRecordFields,
  isEmptyValue,
  shouldOmitForComparison,
  type RawFieldChange,
} from "../../util/compareBlocks";
import { apiWorkflowToSettings } from "../apiWorkflowToSettings";
import {
  applySequentialDefaulting,
  findChainRoot,
} from "../workflowEditorUtils";

export type ReviewStatus = "unchanged" | "new" | "changed" | "removed";

export type ReviewFieldChange = {
  key: string;
  label: string;
  before: string | null;
  after: string | null;
};

export type BlockReview = {
  status: ReviewStatus;
  changes: Array<ReviewFieldChange>;
  // A block nested under this one differs, so this block must stay visible.
  containsChanges: boolean;
};

export type InputReview = {
  key: string;
  status: Exclude<ReviewStatus, "unchanged">;
  detail: string | null;
  changes: Array<ReviewFieldChange>;
};

export type WorkflowReviewDiff = {
  // The proposal's blocks with removed ones threaded back in where they sat.
  mergedBlocks: Array<WorkflowBlock>;
  merged: Map<string, BlockReview>;
  before: Map<string, BlockReview>;
  after: Map<string, BlockReview>;
  // Labels in mergedBlocks of every block that is not unchanged, in run order.
  changeOrder: Array<string>;
  inputs: Array<InputReview>;
  settings: Array<ReviewFieldChange>;
  counts: Record<ReviewStatus, number>;
  isNewWorkflow: boolean;
};

// Matches the canvas node types so task_v2 and navigation blocks with the same
// label are the same block.
const BLOCK_TYPE_TO_NODE_TYPE: Record<string, string> = {
  task: "task",
  task_v2: "navigation",
  validation: "validation",
  action: "action",
  navigation: "navigation",
  extraction: "extraction",
  login: "login",
  wait: "wait",
  terminate: "terminate",
  file_download: "fileDownload",
  code: "codeBlock",
  send_email: "sendEmail",
  text_prompt: "textPrompt",
  for_loop: "loop",
  while_loop: "loop",
  file_url_parser: "fileParser",
  pdf_parser: "pdfParser",
  download_to_s3: "download",
  upload_to_s3: "upload",
  file_upload: "fileUpload",
  goto_url: "url",
  http_request: "http_request",
  pdf_fill: "pdfFill",
  split_pdf: "splitPdf",
  email_inbox: "emailInbox",
  google_sheets_read: "googleSheetsRead",
  google_sheets_write: "googleSheetsWrite",
};

function blockIdentity(block: WorkflowBlock): string {
  const nodeType =
    BLOCK_TYPE_TO_NODE_TYPE[block.block_type] ?? block.block_type;
  return `${nodeType}:${block.label}`;
}

const ACRONYMS = new Set([
  "ai",
  "cdp",
  "css",
  "http",
  "id",
  "ip",
  "json",
  "llm",
  "pdf",
  "sql",
  "totp",
  "url",
]);

const FIELD_LABEL_OVERRIDES: Record<string, string> = {
  wait_sec: "Wait (seconds)",
  loop_variable_reference: "Loop over",
  max_steps_per_run: "Max steps",
  block_type: "Block type",
};

function humanizeKey(key: string): string {
  const override = FIELD_LABEL_OVERRIDES[key];
  if (override) return override;
  const words = key
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .split(/[_\s]+/)
    .filter(Boolean)
    .map((word) => word.toLowerCase())
    .map((word) => (ACRONYMS.has(word) ? word.toUpperCase() : word));
  const [first = "", ...rest] = words;
  return [first.charAt(0).toUpperCase() + first.slice(1), ...rest].join(" ");
}

function formatValue(value: unknown): string | null {
  if (isEmptyValue(value)) return null;
  if (typeof value === "string") return value;
  if (typeof value === "number") return String(value);
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value)) {
    if (
      value.every(
        (item) => typeof item === "string" || typeof item === "number",
      )
    ) {
      return value.join(", ");
    }
    if (
      value.every(
        (item) =>
          item !== null &&
          typeof item === "object" &&
          typeof (item as { key?: unknown }).key === "string",
      )
    ) {
      return value.map((item) => (item as { key: string }).key).join(", ");
    }
  }
  if (
    value !== null &&
    typeof value === "object" &&
    typeof (value as { model_name?: unknown }).model_name === "string"
  ) {
    return (value as { model_name: string }).model_name;
  }
  const stable = stableStringify(value, { omit: shouldOmitForComparison });
  return stable === undefined
    ? null
    : JSON.stringify(JSON.parse(stable), null, 2);
}

// Secret fields render as password inputs in their editors, and the review
// card is plain text, so their values never reach it; a change still shows.
const SECRET_KEY =
  /password|passphrase|secret|private_?key|account_?key|api[_-]?(key|token)|(access|auth|bearer|refresh|session)[_-]?token|^token$|authorization|cookie/i;
const HIDDEN_VALUE = "•••••••• (hidden)";

// Header maps take arbitrary names (X-CSRF-Token, X-Amz-Security-Token, ...),
// so their values are hidden unless the name is known to carry no credential.
const HEADERS_KEY = /headers$/i;
const PUBLIC_HEADERS: ReadonlySet<string> = new Set([
  "accept",
  "accept-encoding",
  "accept-language",
  "cache-control",
  "content-type",
  "origin",
  "referer",
  "user-agent",
]);

function redactHeaders(value: object): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(value).map(([name, item]) => [
      name,
      PUBLIC_HEADERS.has(name.toLowerCase()) || isEmptyValue(item)
        ? item
        : HIDDEN_VALUE,
    ]),
  );
}

function redactSecrets(key: string, value: unknown): unknown {
  if (isEmptyValue(value)) return value;
  if (SECRET_KEY.test(key)) return HIDDEN_VALUE;
  if (HEADERS_KEY.test(key) && typeof value === "object" && value !== null) {
    return Array.isArray(value) ? HIDDEN_VALUE : redactHeaders(value);
  }
  if (Array.isArray(value)) return value.map((item) => redactSecrets("", item));
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([nested, item]) => [
        nested,
        redactSecrets(nested, item),
      ]),
    );
  }
  return value;
}

function toReviewChange(
  change: RawFieldChange,
  label = humanizeKey(change.key),
  hidden = false,
): ReviewFieldChange {
  const shown = (value: unknown) =>
    formatValue(
      hidden && !isEmptyValue(value)
        ? HIDDEN_VALUE
        : redactSecrets(change.key, value),
    );
  return {
    key: change.key,
    label,
    before: shown(change.before),
    after: shown(change.after),
  };
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

type ConditionalLike = {
  branch_conditions?: Array<{
    next_block_label?: string | null;
    criteria?: unknown;
    is_default?: boolean;
  }>;
};

function branchesOf(block: WorkflowBlock) {
  return (block as ConditionalLike).branch_conditions ?? [];
}

// An edge out of a block: its `next_block_label` (branch null) or the target
// of one of its conditional branches.
type Slot = { owner: string; branch: number | null };

function slotTarget(block: WorkflowBlock, branch: number | null) {
  return branch === null
    ? (block.next_block_label ?? null)
    : (branchesOf(block)[branch]?.next_block_label ?? null);
}

function setSlot(
  block: WorkflowBlock,
  branch: number | null,
  target: string | null,
): void {
  if (branch === null) {
    block.next_block_label = target;
    return;
  }
  const condition = branchesOf(block)[branch];
  if (condition) condition.next_block_label = target;
}

// Points every edge into `from` at `to`, so a block inserted before `from`
// takes over its inbound edges, branch targets included.
function repointReferences(
  level: Array<WorkflowBlock>,
  from: string,
  to: string,
): void {
  for (const block of level) {
    if (block.next_block_label === from) {
      block.next_block_label = to;
    }
    for (const branch of (block as ConditionalLike).branch_conditions ?? []) {
      if (branch.next_block_label === from) {
        branch.next_block_label = to;
      }
    }
  }
}

function chainTail(
  level: Array<WorkflowBlock>,
  finallyLabel: string | null,
): WorkflowBlock | null {
  const chain = level.filter((block) => block.label !== finallyLabel);
  const byLabel = new Map(chain.map((block) => [block.label, block]));
  const seen = new Set<string>();
  let cursor = findChainRoot(chain);
  let tail: WorkflowBlock | null = null;
  while (cursor && !seen.has(cursor.label)) {
    seen.add(cursor.label);
    tail = cursor;
    cursor = byLabel.get(cursor.next_block_label ?? "") ?? null;
  }
  return tail ?? chain[chain.length - 1] ?? null;
}

function collectLabels(blocks: Array<WorkflowBlock>, into: Set<string>): void {
  for (const block of blocks) {
    into.add(block.label);
    if (isNestedLoopWorkflowBlock(block)) {
      collectLabels(block.loop_blocks, into);
    }
  }
}

type DiffState = {
  merged: Map<string, BlockReview>;
  before: Map<string, BlockReview>;
  after: Map<string, BlockReview>;
  usedLabels: Set<string>;
};

function uniqueLabel(label: string, used: Set<string>): string {
  let candidate = label;
  for (let n = 1; used.has(candidate); n++) {
    candidate = n === 1 ? `${label} (removed)` : `${label} (removed ${n})`;
  }
  used.add(candidate);
  return candidate;
}

function recordSubtree(
  blocks: Array<WorkflowBlock>,
  status: "new" | "removed",
  state: DiffState,
): void {
  for (const block of blocks) {
    const review: BlockReview = {
      status,
      changes: [],
      containsChanges: false,
    };
    state.merged.set(block.label, review);
    if (status === "new") state.after.set(block.label, review);
    if (isNestedLoopWorkflowBlock(block)) {
      recordSubtree(block.loop_blocks, status, state);
    }
  }
}

// A removed block can share its label with a block Copilot added (a type
// change), and the canvas needs unique labels, so a clashing one is renamed.
function removedCopy(block: WorkflowBlock, state: DiffState): WorkflowBlock {
  const copy = clone(block);
  const relabel = (level: Array<WorkflowBlock>) => {
    const renames = new Map<string, string>();
    for (const item of level) {
      const original = item.label;
      state.before.set(original, {
        status: "removed",
        changes: [],
        containsChanges: false,
      });
      item.label = uniqueLabel(original, state.usedLabels);
      renames.set(original, item.label);
      if (isNestedLoopWorkflowBlock(item)) relabel(item.loop_blocks);
    }
    for (const item of level) {
      const next = item.next_block_label;
      if (next && renames.has(next)) item.next_block_label = renames.get(next);
      for (const branch of branchesOf(item)) {
        const target = branch.next_block_label;
        if (target && renames.has(target)) {
          branch.next_block_label = renames.get(target);
        }
      }
    }
  };
  relabel([copy]);
  recordSubtree([copy], "removed", state);
  return copy;
}

// The order the canvas draws a level in: the next_block_label chain, each
// conditional's branches before its merge point, and the finally block last.
// Array order need not match it.
function runOrder(
  level: Array<WorkflowBlock>,
  finallyLabel: string | null,
): Array<WorkflowBlock> {
  const chain = level.filter((block) => block.label !== finallyLabel);
  const byLabel = new Map(chain.map((block) => [block.label, block]));
  const ordered: Array<WorkflowBlock> = [];
  const seen = new Set<string>();
  const walk = (start: string | null | undefined, stop: string | null) => {
    let cursor = byLabel.get(start ?? "");
    while (cursor && cursor.label !== stop && !seen.has(cursor.label)) {
      seen.add(cursor.label);
      ordered.push(cursor);
      for (const branch of branchesOf(cursor)) {
        walk(branch.next_block_label, cursor.next_block_label ?? null);
      }
      cursor = byLabel.get(cursor.next_block_label ?? "");
    }
  };
  walk(findChainRoot(chain)?.label, null);
  return [
    ...ordered,
    ...chain.filter((block) => !seen.has(block.label)),
    ...level.filter((block) => block.label === finallyLabel),
  ];
}

// The first edge into each block, in run order, so a merge point is entered
// from its conditional rather than from the end of a branch.
function firstInboundSlots(order: Array<WorkflowBlock>): Map<string, Slot> {
  const inbound = new Map<string, Slot>();
  for (const block of order) {
    const slots: Array<number | null> = [null, ...branchesOf(block).keys()];
    for (const branch of slots) {
      const target = slotTarget(block, branch);
      if (target && !inbound.has(target)) {
        inbound.set(target, { owner: block.label, branch });
      }
    }
  }
  return inbound;
}

// Labels reached from a conditional's branches before they rejoin at `merge`.
function branchMembers(
  level: Array<WorkflowBlock>,
  conditional: WorkflowBlock,
  merge: string | null,
): Set<string> {
  const byLabel = new Map(level.map((block) => [block.label, block]));
  const members = new Set<string>();
  const pending = branchesOf(conditional).map((b) => b.next_block_label);
  while (pending.length > 0) {
    const label = pending.pop();
    if (!label || label === merge || members.has(label)) continue;
    const block = byLabel.get(label);
    if (!block) continue;
    members.add(label);
    pending.push(
      block.next_block_label,
      ...branchesOf(block).map((b) => b.next_block_label),
    );
  }
  return members;
}

// Before and after conditionals share a branch when their criteria match, or
// by position when neither side added or dropped a branch.
function matchingBranch(
  prior: WorkflowBlock,
  current: WorkflowBlock,
  branch: number,
): number | null {
  const criteria = (block: WorkflowBlock, index: number) =>
    stableStringify(
      {
        criteria: branchesOf(block)[index]?.criteria ?? null,
        is_default: branchesOf(block)[index]?.is_default ?? false,
      },
      { omit: shouldOmitForComparison },
    );
  if (branchesOf(prior)[branch]?.is_default) {
    const otherElse = branchesOf(current).findIndex((b) => b.is_default);
    if (otherElse >= 0) return otherElse;
  }
  const wanted = criteria(prior, branch);
  const byCriteria = branchesOf(current).findIndex(
    (_, index) => criteria(current, index) === wanted,
  );
  if (byCriteria >= 0) return byCriteria;
  return branchesOf(prior).length === branchesOf(current).length
    ? branch
    : null;
}

// Where a branch leads, read as the first block it reaches that both versions
// share, so a block added at the head of a branch does not read as a re-route.
function branchRoute(
  level: Array<WorkflowBlock>,
  start: string | null | undefined,
  merge: string | null,
  shared: (block: WorkflowBlock) => boolean,
): string | null {
  const byLabel = new Map(level.map((block) => [block.label, block]));
  const seen = new Set<string>();
  let cursor = byLabel.get(start ?? "");
  while (cursor && cursor.label !== merge && !seen.has(cursor.label)) {
    if (shared(cursor)) return cursor.label;
    seen.add(cursor.label);
    cursor = byLabel.get(cursor.next_block_label ?? "");
  }
  return null;
}

function describeRoutes(
  block: WorkflowBlock,
  routes: Array<string | null>,
): string {
  return routes
    .map((route, index) => {
      const name = branchesOf(block)[index]?.is_default
        ? "Else"
        : `Branch ${index + 1}`;
      return `${name} → ${route ?? "end"}`;
    })
    .join(", ");
}

// The proposal dropped this branch, so the review canvas keeps a copy of it
// (ahead of Else, or as Else when the proposal has none); the removed blocks
// that ran there stay inside the conditional instead of reading as steps
// after its merge point.
function keepDroppedBranch(
  prior: WorkflowBlock,
  current: WorkflowBlock,
  branch: number,
): number | null {
  const dropped = branchesOf(prior)[branch];
  const conditions = (current as ConditionalLike).branch_conditions;
  if (!dropped || !conditions) return null;
  const elseIndex = conditions.findIndex((condition) => condition.is_default);
  // A conditional allows one Else at most.
  if (dropped.is_default && elseIndex >= 0) return null;
  const copy = {
    ...clone(dropped),
    id: `${(dropped as { id?: string }).id ?? "branch"}-removed`,
    next_block_label: null,
  };
  conditions.splice(elseIndex < 0 ? conditions.length : elseIndex, 0, copy);
  return conditions.indexOf(copy);
}

function mergeLevel(
  beforeLevel: Array<WorkflowBlock>,
  afterLevel: Array<WorkflowBlock>,
  finallyLabels: { before: string | null; after: string | null },
  state: DiffState,
): Array<WorkflowBlock> {
  const finallyLabel = finallyLabels.after;
  const beforeByIdentity = new Map(
    beforeLevel.map((block) => [blockIdentity(block), block]),
  );
  const afterIdentities = new Set(afterLevel.map(blockIdentity));
  const beforeOrder = runOrder(beforeLevel, finallyLabels.before);
  const merged = afterLevel.map((block) => {
    const prior = beforeByIdentity.get(blockIdentity(block));
    if (!prior) {
      recordSubtree([block], "new", state);
      return block;
    }
    const changes = diffBlockFields(prior, block).map((change) =>
      toReviewChange(change),
    );
    if (branchesOf(block).length > 0) {
      const beforeRoutes = branchesOf(prior).map((branch) =>
        branchRoute(
          beforeLevel,
          branch.next_block_label,
          prior.next_block_label ?? null,
          (candidate) => afterIdentities.has(blockIdentity(candidate)),
        ),
      );
      const afterRoutes = branchesOf(block).map((branch) =>
        branchRoute(
          afterLevel,
          branch.next_block_label,
          block.next_block_label ?? null,
          (candidate) => beforeByIdentity.has(blockIdentity(candidate)),
        ),
      );
      const rerouted = afterRoutes.some((route, index) => {
        const match = branchesOf(prior).findIndex(
          (_, priorIndex) => matchingBranch(prior, block, priorIndex) === index,
        );
        return match < 0 || beforeRoutes[match] !== route;
      });
      if (rerouted) {
        changes.push({
          key: "__branch_routes",
          label: "Branch routes",
          before: describeRoutes(prior, beforeRoutes),
          after: describeRoutes(block, afterRoutes),
        });
      }
    }
    const review: BlockReview = {
      status: changes.length > 0 ? "changed" : "unchanged",
      changes,
      containsChanges: false,
    };
    state.merged.set(block.label, review);
    state.before.set(prior.label, review);
    state.after.set(block.label, review);
    if (isNestedLoopWorkflowBlock(block) && isNestedLoopWorkflowBlock(prior)) {
      block.loop_blocks = mergeLevel(
        prior.loop_blocks,
        block.loop_blocks,
        { before: null, after: null },
        state,
      );
    }
    return block;
  });

  // Each removed block goes back on the edge that led into it: it takes that
  // edge over from whatever the proposal points it at now.
  const inbound = firstInboundSlots(beforeOrder);
  const beforeRoot = beforeOrder[0]?.label;
  const placed = new Map<string, WorkflowBlock>(
    beforeLevel.flatMap((prior) => {
      const kept = merged.find(
        (block) => blockIdentity(block) === blockIdentity(prior),
      );
      return kept ? [[prior.label, kept] as const] : [];
    }),
  );
  const removedCopies = new Set<WorkflowBlock>();
  const placeAfter = (slot: Slot, removed: WorkflowBlock): boolean => {
    const owner = placed.get(slot.owner);
    const prior = beforeLevel.find((block) => block.label === slot.owner);
    if (!owner || !prior) return false;
    const branch =
      slot.branch === null || removedCopies.has(owner)
        ? slot.branch
        : (matchingBranch(prior, owner, slot.branch) ??
          keepDroppedBranch(prior, owner, slot.branch));
    if (slot.branch !== null && branch === null) return false;
    const target = slotTarget(owner, branch);
    // Entering a conditional's merge point: its branches must rejoin at the
    // removed block too, or they would run on past it.
    if (branch === null && branchesOf(owner).length > 0) {
      for (const label of branchMembers(merged, owner, target)) {
        const member = merged.find((block) => block.label === label);
        if (member && member.next_block_label === target) {
          member.next_block_label = removed.label;
        }
      }
    }
    setSlot(owner, branch, removed.label);
    removed.next_block_label = target;
    merged.splice(merged.indexOf(owner) + 1, 0, removed);
    return true;
  };

  for (const prior of beforeOrder) {
    if (afterIdentities.has(blockIdentity(prior))) continue;
    const removed = removedCopy(prior, state);
    // Its old branch targets are stale; removed blocks from those branches
    // attach again as they are placed.
    branchesOf(removed).forEach((branch) => {
      branch.next_block_label = null;
    });
    removedCopies.add(removed);
    placed.set(prior.label, removed);
    const slot = inbound.get(prior.label);
    if (slot && placeAfter(slot, removed)) continue;
    if (prior.label === beforeRoot && prior.label !== finallyLabels.before) {
      const root = runOrder(merged, finallyLabel)[0];
      removed.next_block_label =
        root && root.label !== finallyLabel ? root.label : null;
      merged.unshift(removed);
      continue;
    }
    const tail = chainTail(merged, finallyLabel);
    if (tail) tail.next_block_label = removed.label;
    removed.next_block_label = null;
    merged.push(removed);
  }

  return merged;
}

function markContainedChanges(
  blocks: Array<WorkflowBlock>,
  reviews: Map<string, BlockReview>,
): boolean {
  let any = false;
  for (const block of blocks) {
    const review = reviews.get(block.label);
    let nested = false;
    if (isNestedLoopWorkflowBlock(block)) {
      nested = markContainedChanges(block.loop_blocks, reviews);
    }
    if (review) review.containsChanges = nested;
    if (nested || (review && review.status !== "unchanged")) any = true;
  }
  return any;
}

function changeOrderOf(
  blocks: Array<WorkflowBlock>,
  reviews: Map<string, BlockReview>,
  finallyLabel: string | null,
): Array<string> {
  return runOrder(blocks, finallyLabel).flatMap((block) => [
    ...(reviews.get(block.label)?.status !== "unchanged" ? [block.label] : []),
    ...(isNestedLoopWorkflowBlock(block)
      ? changeOrderOf(block.loop_blocks, reviews, null)
      : []),
  ]);
}

function reviewableInputs(version: WorkflowVersion): Map<string, Parameter> {
  return new Map(
    (version.workflow_definition?.parameters ?? [])
      .filter((parameter) => parameter.parameter_type !== "output")
      .map((parameter) => [parameter.key, parameter]),
  );
}

function inputDetail(parameter: Parameter): string | null {
  if (parameter.parameter_type === "workflow") {
    return parameter.workflow_parameter_type;
  }
  return humanizeKey(parameter.parameter_type);
}

const INPUT_IGNORED_KEYS: ReadonlySet<string> = new Set(["key"]);

function diffInputs(
  before: WorkflowVersion,
  after: WorkflowVersion,
): Array<InputReview> {
  const beforeInputs = reviewableInputs(before);
  const afterInputs = reviewableInputs(after);
  const reviews: Array<InputReview> = [];
  afterInputs.forEach((parameter, key) => {
    const prior = beforeInputs.get(key);
    if (!prior) {
      reviews.push({
        key,
        status: "new",
        detail: inputDetail(parameter),
        changes: [],
      });
      return;
    }
    const changes = diffRecordFields(
      prior as unknown as Record<string, unknown>,
      parameter as unknown as Record<string, unknown>,
      INPUT_IGNORED_KEYS,
    ).map((change) => toReviewChange(change, undefined, SECRET_KEY.test(key)));
    if (changes.length > 0) {
      reviews.push({
        key,
        status: "changed",
        detail: inputDetail(parameter),
        changes,
      });
    }
  });
  beforeInputs.forEach((parameter, key) => {
    if (!afterInputs.has(key)) {
      reviews.push({
        key,
        status: "removed",
        detail: inputDetail(parameter),
        changes: [],
      });
    }
  });
  return reviews;
}

// Labels match the workflow settings editor, so a change reads the way the
// setting is named where the user would edit it.
const SETTING_LABELS: Record<keyof WorkflowSettings, string> = {
  model: "Model",
  workflowSystemPrompt: "Agent system prompt",
  webhookCallbackUrl: "Webhook callback URL",
  proxyLocation: "Proxy location",
  browserType: "Browser type",
  runWith: "Run with",
  aiFallback: "AI fallback (cached scripts)",
  scriptCacheKey: "Code key",
  codeVersion: "Code version",
  maskSecrets: "Mask secrets",
  runSequentially: "Prevent overlapping runs",
  sequentialKey: "Sequential key",
  reuseBrowserSession: "Reuse browser session",
  persistBrowserSession: "Save & reuse browser profile",
  pinSavedSessionIp: "Keep same IP across runs",
  browserProfileKey: "Browser profile key",
  browserProfileId: "Starting browser profile",
  extraHttpHeaders: "Extra HTTP headers",
  cdpConnectHeaders: "CDP connect headers",
  maxScreenshotScrolls: "Max screenshot scrolls",
  maxElapsedTimeMinutes: "Max run time (minutes)",
  finallyBlockLabel: "Execute on any outcome",
  totpVerificationUrl: "2FA verification URL",
  totpIdentifier: "2FA identifier",
  adaptiveCaching: "Adaptive caching",
  generateScriptOnTerminal: "Generate script on terminal",
  errorCodeMapping: "Error code mapping",
  retryPolicy: "Retry policy",
};

const JSON_STRING_SETTINGS: ReadonlySet<keyof WorkflowSettings> = new Set([
  "extraHttpHeaders",
  "cdpConnectHeaders",
]);

function comparableSettings(version: WorkflowVersion): Record<string, unknown> {
  const settings: Record<string, unknown> = {
    ...apiWorkflowToSettings(version),
  };
  for (const key of JSON_STRING_SETTINGS) {
    const raw = settings[key];
    if (typeof raw === "string") {
      try {
        settings[key] = JSON.parse(raw);
      } catch {
        // Unparseable headers compare as their raw text.
      }
    }
  }
  // The code version only applies when running with code; the pre-submit
  // snapshot clears it otherwise while the server keeps its default.
  if (settings.runWith !== "code") settings.codeVersion = null;
  return settings;
}

const METADATA_LABELS = { title: "Title", description: "Description" };

function diffSettings(
  before: WorkflowVersion,
  after: WorkflowVersion,
): Array<ReviewFieldChange> {
  // Accept applies a new title or description too, so it reviews with the
  // settings rather than leaving the header to say "No changes".
  const metadata = (["title", "description"] as const)
    .filter((key) => !areValuesEquivalent(before[key], after[key]))
    .map((key) =>
      toReviewChange(
        { key, before: before[key], after: after[key] },
        METADATA_LABELS[key],
      ),
    );
  const beforeSettings = comparableSettings(before);
  const afterSettings = comparableSettings(after);
  return [
    ...metadata,
    ...(Object.keys(SETTING_LABELS) as Array<keyof WorkflowSettings>)
      .filter(
        (key) => !areValuesEquivalent(beforeSettings[key], afterSettings[key]),
      )
      .map((key) =>
        toReviewChange(
          { key, before: beforeSettings[key], after: afterSettings[key] },
          SETTING_LABELS[key],
          // Headers that failed to parse stay raw text, out of reach of the
          // per-header redaction.
          JSON_STRING_SETTINGS.has(key) &&
            (typeof beforeSettings[key] === "string" ||
              typeof afterSettings[key] === "string"),
        ),
      ),
  ];
}

function diffWorkflowVersions(
  before: WorkflowVersion,
  after: WorkflowVersion,
): WorkflowReviewDiff {
  const finallyLabel = after.workflow_definition?.finally_block_label ?? null;
  const beforeFinallyLabel =
    before.workflow_definition?.finally_block_label ?? null;
  const beforeBlocks = applySequentialDefaulting(
    clone(before.workflow_definition?.blocks ?? []),
    beforeFinallyLabel,
  );
  const afterBlocks = applySequentialDefaulting(
    clone(after.workflow_definition?.blocks ?? []),
    finallyLabel,
  );
  const usedLabels = new Set<string>();
  collectLabels(afterBlocks, usedLabels);
  const state: DiffState = {
    merged: new Map(),
    before: new Map(),
    after: new Map(),
    usedLabels,
  };
  const mergedBlocks = mergeLevel(
    beforeBlocks,
    afterBlocks,
    { before: beforeFinallyLabel, after: finallyLabel },
    state,
  );
  markContainedChanges(mergedBlocks, state.merged);

  const counts: Record<ReviewStatus, number> = {
    unchanged: 0,
    new: 0,
    changed: 0,
    removed: 0,
  };
  state.merged.forEach((review) => {
    counts[review.status] += 1;
  });

  return {
    mergedBlocks,
    merged: state.merged,
    before: state.before,
    after: state.after,
    changeOrder: changeOrderOf(mergedBlocks, state.merged, finallyLabel),
    inputs: diffInputs(before, after),
    settings: diffSettings(before, after),
    counts,
    isNewWorkflow: beforeBlocks.length === 0 && afterBlocks.length > 0,
  };
}

export type UnchangedFold = {
  key: string;
  count: number;
};

const FOLD_LABEL_PREFIX = "__review_fold__";

function isQuiet(
  block: WorkflowBlock,
  reviews: Map<string, BlockReview>,
  finallyLabel: string | null,
): boolean {
  const review = reviews.get(block.label);
  return (
    block.label !== finallyLabel &&
    review?.status === "unchanged" &&
    !review.containsChanges
  );
}

// The fold marker rides the canvas as a wait block, the simplest block the
// renderer lays out; the review wrapper swaps its card for the marker.
function foldPlaceholder(
  key: string,
  next: string | null | undefined,
): WorkflowBlock {
  return {
    block_type: "wait",
    label: `${FOLD_LABEL_PREFIX}${key}`,
    wait_sec: 0,
    continue_on_failure: false,
    model: null,
    next_block_label: next ?? null,
  } as unknown as WorkflowBlock;
}

function foldLevel(
  level: Array<WorkflowBlock>,
  reviews: Map<string, BlockReview>,
  expanded: ReadonlySet<string>,
  finallyLabel: string | null,
  folds: Map<string, UnchangedFold>,
): Array<WorkflowBlock> {
  const withChildren = level.map((block) =>
    isNestedLoopWorkflowBlock(block)
      ? ({
          ...block,
          loop_blocks: foldLevel(
            block.loop_blocks,
            reviews,
            expanded,
            null,
            folds,
          ),
        } as WorkflowBlock)
      : { ...block },
  );
  // Branch targets reference blocks by label across the level, so a fold here
  // could orphan a branch.
  if (withChildren.some((block) => block.block_type === "conditional")) {
    return withChildren;
  }

  const runs: Array<Array<WorkflowBlock>> = [];
  let run: Array<WorkflowBlock> = [];
  for (const block of runOrder(withChildren, finallyLabel)) {
    const extendsRun =
      run.length > 0 && run[run.length - 1]!.next_block_label === block.label;
    if (
      isQuiet(block, reviews, finallyLabel) &&
      (run.length === 0 || extendsRun)
    ) {
      run.push(block);
      continue;
    }
    runs.push(run);
    run = isQuiet(block, reviews, finallyLabel) ? [block] : [];
  }
  runs.push(run);

  const placeholders = new Map<string, WorkflowBlock>();
  const foldedLabels = new Set<string>();
  for (const candidate of runs) {
    const key = candidate[0]?.label;
    // Any run folds, even a single block: one block card can outgrow the pane.
    if (!key || expanded.has(key)) {
      continue;
    }
    const last = candidate[candidate.length - 1]!;
    const placeholder = foldPlaceholder(key, last.next_block_label);
    placeholders.set(key, placeholder);
    candidate.forEach((block) => foldedLabels.add(block.label));
    folds.set(placeholder.label, { key, count: candidate.length });
  }

  const result: Array<WorkflowBlock> = [];
  for (const block of withChildren) {
    const placeholder = placeholders.get(block.label);
    if (placeholder) result.push(placeholder);
    if (!foldedLabels.has(block.label)) result.push(block);
  }
  placeholders.forEach((placeholder, key) =>
    repointReferences(result, key, placeholder.label),
  );
  return result;
}

function foldUnchangedRuns(
  blocks: Array<WorkflowBlock>,
  reviews: Map<string, BlockReview>,
  expanded: ReadonlySet<string>,
  finallyLabel: string | null,
): { blocks: Array<WorkflowBlock>; folds: Map<string, UnchangedFold> } {
  const folds = new Map<string, UnchangedFold>();
  return {
    blocks: foldLevel(blocks, reviews, expanded, finallyLabel, folds),
    folds,
  };
}

export type TextSegment = { text: string; changed: boolean };

// Past this many token pairs the LCS table gets expensive to build on every
// render, and a rewrite that large reads better as a plain before/after anyway.
const MAX_WORD_DIFF_CELLS = 40_000;

function pushSegment(
  segments: Array<TextSegment>,
  text: string,
  changed: boolean,
) {
  const last = segments[segments.length - 1];
  if (last && last.changed === changed) {
    last.text += text;
  } else {
    segments.push({ text, changed });
  }
}

function diffWords(
  before: string,
  after: string,
): { before: Array<TextSegment>; after: Array<TextSegment> } {
  const tokenize = (text: string) =>
    text.match(/\s+|[\p{L}\p{N}_]+|[^\s\p{L}\p{N}_]/gu) ?? [];
  const a = tokenize(before);
  const b = tokenize(after);
  // Only the span between the shared start and end needs the LCS table, which
  // keeps a one-clause edit to a long prompt well under the cap.
  let prefix = 0;
  while (prefix < a.length && prefix < b.length && a[prefix] === b[prefix]) {
    prefix++;
  }
  let suffix = 0;
  while (
    suffix < a.length - prefix &&
    suffix < b.length - prefix &&
    a[a.length - 1 - suffix] === b[b.length - 1 - suffix]
  ) {
    suffix++;
  }
  const midA = a.slice(prefix, a.length - suffix);
  const midB = b.slice(prefix, b.length - suffix);
  const beforeSegments: Array<TextSegment> = [];
  const afterSegments: Array<TextSegment> = [];
  const shared = (tokens: Array<string>) => {
    for (const token of tokens) {
      pushSegment(beforeSegments, token, false);
      pushSegment(afterSegments, token, false);
    }
  };
  shared(a.slice(0, prefix));
  if (midA.length * midB.length > MAX_WORD_DIFF_CELLS) {
    if (midA.length > 0) pushSegment(beforeSegments, midA.join(""), true);
    if (midB.length > 0) pushSegment(afterSegments, midB.join(""), true);
  } else {
    const lcs: Array<Array<number>> = Array.from(
      { length: midA.length + 1 },
      () => new Array<number>(midB.length + 1).fill(0),
    );
    for (let i = midA.length - 1; i >= 0; i--) {
      for (let j = midB.length - 1; j >= 0; j--) {
        lcs[i]![j] =
          midA[i] === midB[j]
            ? lcs[i + 1]![j + 1]! + 1
            : Math.max(lcs[i + 1]![j]!, lcs[i]![j + 1]!);
      }
    }
    let i = 0;
    let j = 0;
    while (i < midA.length && j < midB.length) {
      if (midA[i] === midB[j]) {
        pushSegment(beforeSegments, midA[i]!, false);
        pushSegment(afterSegments, midB[j]!, false);
        i++;
        j++;
      } else if (lcs[i + 1]![j]! >= lcs[i]![j + 1]!) {
        pushSegment(beforeSegments, midA[i]!, true);
        i++;
      } else {
        pushSegment(afterSegments, midB[j]!, true);
        j++;
      }
    }
    for (; i < midA.length; i++) pushSegment(beforeSegments, midA[i]!, true);
    for (; j < midB.length; j++) pushSegment(afterSegments, midB[j]!, true);
  }
  shared(a.slice(a.length - suffix));
  return { before: beforeSegments, after: afterSegments };
}

export {
  blockIdentity,
  diffWords,
  diffWorkflowVersions,
  foldUnchangedRuns,
  humanizeKey,
};
