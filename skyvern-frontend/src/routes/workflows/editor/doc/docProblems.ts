import { isLoginBlockMissingCredentials } from "../../runValidation";
import { isWorkflowBlockNode, type AppNode } from "../nodes";

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

/** The array fields of a step's data schema a loop can go through as `<label>_output.extracted_information.<name>`. */
export function recordListFields(node: AppNode): string[] {
  if (!isWorkflowBlockNode(node) || !node.type) return [];
  if (!EXTRACTED_INFORMATION_STEPS.has(node.type)) return [];
  const raw = (node.data as Record<string, unknown>).dataSchema;
  let schema: unknown;
  try {
    schema = typeof raw === "string" ? JSON.parse(raw) : raw;
  } catch {
    return [];
  }
  const properties = (schema as { properties?: unknown } | null)?.properties;
  if (!properties || typeof properties !== "object") return [];
  // Only names a `{{ }}` path can hold without quoting.
  return Object.entries(properties as Record<string, { type?: unknown }>)
    .filter(
      ([name, field]) =>
        dottedFieldReadable(name) &&
        (field?.type === "array" ||
          (Array.isArray(field?.type) && field.type.includes("array"))),
    )
    .map(([name]) => name);
}

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
  return label && path ? { label, path } : null;
}

function wholeRecordProblem({
  label,
  path,
}: NonNullable<ReturnType<typeof wholeRecordLoopSource>>) {
  const example = `${label}_output.${path}`;
  if (path === "extracted_information.<field>")
    return `Pick the list inside ${label}_output, for example ${example}, with <field> listed in ${label}'s data schema. Use ${label}_output.extracted_information only if that data schema is an array.`;
  return `Pick the list inside ${label}_output, for example ${example}.`;
}

/** What a step still needs before it can run, found from the draft as it is typed. */
export function docStepProblems(nodes: AppNode[]): Map<string, string[]> {
  const problems = new Map<string, string[]>();
  for (const node of nodes) {
    if (!isWorkflowBlockNode(node)) continue;
    const data = node.data as Record<string, unknown>;
    const found: string[] = [];
    if (
      node.type === "loop" &&
      data.loopKind !== "while" &&
      !text(data.loopVariableReference) &&
      !text(data.loopValue)
    )
      found.push("Pick the list to go through with @");
    const wholeRecord =
      node.type === "loop" && data.loopKind !== "while"
        ? wholeRecordLoopSource(text(data.loopVariableReference), nodes)
        : null;
    if (wholeRecord) found.push(wholeRecordProblem(wholeRecord));
    // ASCII `\w` is enough here: only ASCII names are keywords or dict attributes.
    const methodField =
      node.type === "loop" && data.loopKind !== "while"
        ? /\.extracted_information\.(\w+)[\s}]*$/.exec(
            text(data.loopVariableReference),
          )?.[1]
        : undefined;
    if (methodField && !dottedFieldReadable(methodField))
      found.push(
        `Rename the list field "${methodField}" in the earlier step's data schema. A loop can't go through a field with that name.`,
      );
    if (node.type === "url" && !text(data.url))
      found.push("Set the URL: type it on the line, or use @");
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
      found.push("Choose a saved credential in this step's settings");
    if (found.length) problems.set(node.id, found);
  }
  return problems;
}
