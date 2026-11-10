import { isLoginBlockMissingCredentials } from "../../runValidation";
import { isWorkflowBlockNode, type AppNode } from "../nodes";
import { docStepCaption, loopHasNoList } from "./docStepRegistry";
import type { DocItem } from "./buildDocTree";

const text = (value: unknown) =>
  typeof value === "string" ? value.trim() : "";

// Mirrors templating.py `_extend_path`: dotted access only for an identifier that is not a keyword,
// true/false/none (any case) or a dict attribute, which Jinja would read instead of the field.
const jinjaReserved = new Set(
  "and as assert async await break class continue def del elif else except false finally for from global if import in is lambda none nonlocal not or pass raise return true try while with yield".split(
    " ",
  ),
);
// A snapshot of `dir(dict)`; a superset only declines a dotted path the backend would accept.
const dictAttributes = new Set(
  "__class__ __class_getitem__ __contains__ __delattr__ __delitem__ __dir__ __doc__ __eq__ __format__ __ge__ __getattribute__ __getitem__ __getstate__ __gt__ __hash__ __init__ __init_subclass__ __ior__ __iter__ __le__ __len__ __lt__ __ne__ __new__ __or__ __reduce__ __reduce_ex__ __repr__ __reversed__ __ror__ __setattr__ __setitem__ __sizeof__ __str__ __subclasshook__ clear copy fromkeys get items keys pop popitem setdefault update values".split(
    " ",
  ),
);
export const dottedFieldReadable = (name: string) =>
  /^[\p{XID_Start}_]\p{XID_Continue}*$/u.test(name) &&
  !jinjaReserved.has(name.toLowerCase()) &&
  !dictAttributes.has(name);

/** Steps whose result keeps what they extracted under `extracted_information`, shaped by their data schema. */
const EXTRACTED_INFORMATION_STEPS = new Set([
  "extraction",
  "task",
  "navigation",
]);

// Steps whose output is always a record; the list to go through sits under one of its keys.
const recordListPaths: Record<string, string> = {
  extraction: "extracted_information.<field>",
  task: "extracted_information.<field>",
  navigation: "extracted_information.<field>",
  http_request: "body",
  googleSheetsRead: "rows",
  emailInbox: "emails",
  web_search: "results",
};

export const stepResultIsRecord = (type: string | undefined) =>
  type !== undefined && recordListPaths[type] !== undefined;

function dataSchemaOf(
  node: AppNode,
): { type?: unknown; properties?: unknown } | null {
  if (!isWorkflowBlockNode(node) || !node.type) return null;
  if (!EXTRACTED_INFORMATION_STEPS.has(node.type)) return null;
  const raw = (node.data as Record<string, unknown>).dataSchema;
  try {
    const schema = typeof raw === "string" ? JSON.parse(raw) : raw;
    return schema && typeof schema === "object" ? schema : null;
  } catch {
    return null;
  }
}

const schemaUnparsable = (node: AppNode) => {
  const raw = (node.data as Record<string, unknown>).dataSchema;
  if (typeof raw !== "string" || !raw.trim()) return false;
  try {
    JSON.parse(raw);
    return false;
  } catch {
    return true;
  }
};

// Like the backend's `resolve_schema_type`, a union takes its first non-null type: `["array", "null"]` is an array.
const isArrayType = (type: unknown) =>
  (Array.isArray(type)
    ? type.find((t) => String(t).toLowerCase() !== "null")
    : type
  )
    ?.toString()
    .toLowerCase() === "array";

const schemaIsList = (node: AppNode) => isArrayType(dataSchemaOf(node)?.type);

function arrayFields(node: AppNode): string[] {
  const properties = dataSchemaOf(node)?.properties;
  if (!properties || typeof properties !== "object") return [];
  return Object.entries(properties as Record<string, { type?: unknown }>)
    .filter(([, field]) => isArrayType(field?.type))
    .map(([name]) => name);
}

/** The array fields of a step's data schema a loop can go through as `<label>_output.extracted_information.<name>`. */
export function recordListFields(node: AppNode): string[] {
  // Only names a `{{ }}` path can hold without quoting.
  return arrayFields(node).filter(dottedFieldReadable);
}

const renameListField = (field: string) =>
  `Rename the list field "${field}" in the earlier step's data schema. A loop can't go through a field with that name.`;

// The run strips spaces and braces from the reference (block.py `strip(' {}')`), so `{{ X_output }}`,
// `{{X_output}}`, `X_output`, `{{ X }}` and `X` all resolve to X's whole result record.
function wholeRecordLoopSource(reference: string, nodes: AppNode[]) {
  const name = reference.replace(/^[\s{}]+|[\s{}]+$/g, "");
  if (!/^[A-Za-z0-9_]+$/.test(name)) return null;
  // The name as a label first, so a label that itself ends in `_output` still resolves.
  const label = [name, name.replace(/_output$/, "")].find((candidate) =>
    nodes.some(
      (node) => isWorkflowBlockNode(node) && node.data.label === candidate,
    ),
  );
  const source = nodes.find(
    (node) => isWorkflowBlockNode(node) && node.data.label === label,
  );
  // With the header row off, a Sheets read records the list under `values` and leaves `rows` empty.
  const path =
    source?.type === "googleSheetsRead" &&
    (source.data as Record<string, unknown>).hasHeaderRow === false
      ? "values"
      : source?.type
        ? recordListPaths[source.type]
        : undefined;
  return label && source && path ? { label, path, source } : null;
}

type WholeRecordSource = NonNullable<ReturnType<typeof wholeRecordLoopSource>>;

// A step with a Data Schema field but no top-level list in it leaves `@` nothing to offer but the whole result.
const hasNoSchemaList = ({ path, source }: WholeRecordSource) =>
  path === "extracted_information.<field>" &&
  source.type !== "navigation" &&
  !schemaUnparsable(source) &&
  !schemaIsList(source) &&
  arrayFields(source).length === 0;

function wholeRecordProblem(found: WholeRecordSource) {
  const { label, path, source } = found;
  const name = docStepCaption(source) ?? label;
  if (hasNoSchemaList(found))
    return `${name} has no top-level list in its Data Schema, so the loop has nothing to go through. Add a list field at the top level of its Data Schema, then pick it here with @.`;
  const whole = `${label}_output is the whole result of ${name}, not a list`;
  if (source.type === "navigation")
    return `${whole}, and this step's result usually holds no list. Pick a list from an earlier extraction with @.`;
  if (path !== "extracted_information.<field>")
    return `${whole}. Pick the list inside it, for example ${label}_output.${path}.`;
  if (schemaIsList(source))
    return `${whole}. Its Data Schema is a list, so use ${label}_output.extracted_information.`;
  const unreadable = arrayFields(source)[0];
  if (unreadable !== undefined && !recordListFields(source).length)
    return renameListField(unreadable);
  return `${whole}. Pick one of its list fields with @.`;
}

const loopSource = (node: AppNode, nodes: AppNode[]) => {
  const data = node.data as Record<string, unknown>;
  return node.type === "loop" && data.loopKind !== "while"
    ? wholeRecordLoopSource(text(data.loopVariableReference), nodes)
    : null;
};

/** The earlier step whose Data Schema needs a list field before this loop can go through it, if that is the loop's problem. */
export function loopSchemaStepId(loop: AppNode, nodes: AppNode[]) {
  const found = loopSource(loop, nodes);
  return found && hasNoSchemaList(found) ? found.source.id : null;
}

/** What a step still needs before it can run, found from the draft as it is typed. */
export function docStepProblems(nodes: AppNode[]): Map<string, string[]> {
  const problems = new Map<string, string[]>();
  for (const node of nodes) {
    if (!isWorkflowBlockNode(node)) continue;
    const data = node.data as Record<string, unknown>;
    const found: string[] = [];
    if (node.type === "loop" && loopHasNoList(data))
      found.push(
        text(data.description)
          ? "This line names the list in words; the loop needs the list itself. Type @ on the line and pick it."
          : "This loop has no list to go through. Type @ on its line and pick a list.",
      );
    const wholeRecord = loopSource(node, nodes);
    if (wholeRecord) found.push(wholeRecordProblem(wholeRecord));
    // ASCII `\w` is enough here: only ASCII names are keywords or dict attributes.
    const methodField =
      node.type === "loop" && data.loopKind !== "while"
        ? /\.extracted_information\.(\w+)[\s}]*$/.exec(
            text(data.loopVariableReference),
          )?.[1]
        : undefined;
    if (methodField && !dottedFieldReadable(methodField))
      found.push(renameListField(methodField));
    if (node.type === "url" && !text(data.url))
      found.push("This step has no URL. Type it on the line, or use @.");
    if (
      node.type === "login" &&
      isLoginBlockMissingCredentials({
        block_type: "login",
        label: node.data.label,
        url: text(data.url),
        parameter_keys: Array.isArray(data.parameterKeys)
          ? (data.parameterKeys as string[])
          : [],
      })
    )
      found.push(
        "This step has no saved credential. Open Step options, then Edit in side panel to choose one.",
      );
    if (found.length) problems.set(node.id, found);
  }
  return problems;
}

export type DocProblemEntry = {
  key: string;
  nodeId: string | null;
  message: string;
  /** Holds Run; the Run toast counts exactly these entries. */
  blocking: boolean;
};

function stepOrder(items: DocItem[], order: Map<string, number>) {
  for (const item of items) {
    if (item.kind === "note") continue;
    if (item.kind === "unplaced") {
      for (const id of item.nodeIds) order.set(id, order.size);
      continue;
    }
    order.set(item.nodeId, order.size);
    if (item.kind === "loop") stepOrder(item.children, order);
    if (item.kind === "conditional")
      for (const branch of item.branches) stepOrder(branch.children, order);
  }
  return order;
}

/** One entry per distinct fix line and server problem of a step, in reading order; the header, list and Run all count these. */
export function docProblemEntries({
  nodes,
  items,
  fixes,
  problems,
}: {
  nodes: AppNode[];
  items: DocItem[];
  fixes: ReadonlyMap<string, string[]>;
  problems: ReadonlyArray<{
    block_label: string | null;
    code: string;
    message: string;
    severity?: "error" | "warning";
  }>;
}): DocProblemEntry[] {
  const idsByLabel = new Map(nodes.map((node) => [node.data.label, node.id]));
  // A check that couldn't reach the server leaves problems from an older draft: shown, but not holding Run.
  const checkFailed = problems.some(
    (problem) => problem.code === "validation_failed",
  );
  const byIdentity = new Map<string, DocProblemEntry>();
  for (const entry of [
    ...[...fixes].flatMap(([nodeId, messages]) =>
      messages.map((message, i) => ({
        key: `${nodeId}:fix:${i}`,
        nodeId,
        message,
        blocking: true,
      })),
    ),
    ...problems.map((problem, i) => ({
      key: `server:${i}`,
      nodeId: idsByLabel.get(problem.block_label ?? "") ?? null,
      message: problem.message,
      // A credential named in a note is a save warning, but the run would read it, so it holds Run.
      blocking:
        !checkFailed &&
        problem.code !== "validation_failed" &&
        (problem.severity !== "warning" ||
          problem.code === "credential_in_note"),
    })),
  ]) {
    const id = `${entry.nodeId}\n${entry.message}`;
    const previous = byIdentity.get(id);
    if (previous) previous.blocking ||= entry.blocking;
    else byIdentity.set(id, entry);
  }
  const entries = [...byIdentity.values()];
  const order = stepOrder(items, new Map());
  const rank = (entry: DocProblemEntry) =>
    entry.nodeId === null ? -1 : (order.get(entry.nodeId) ?? order.size);
  return entries.sort((a, b) => rank(a) - rank(b));
}

export function docProblemsTitle(entries: DocProblemEntry[]) {
  const blocking = entries.filter((entry) => entry.blocking).length;
  const warnings = entries.length - blocking;
  const plural = (n: number, word: string) =>
    `${n} ${word}${n === 1 ? "" : "s"}`;
  return [
    blocking && plural(blocking, "problem"),
    warnings && plural(warnings, "warning"),
  ]
    .filter(Boolean)
    .join(", ");
}
