import { isLoginBlockMissingCredentials } from "../../runValidation";
import { isWorkflowBlockNode, type AppNode } from "../nodes";

const text = (value: unknown) =>
  typeof value === "string" ? value.trim() : "";

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
