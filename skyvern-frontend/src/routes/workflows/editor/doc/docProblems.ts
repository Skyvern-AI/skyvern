import { isLoginBlockMissingCredentials } from "../../runValidation";
import { isWorkflowBlockNode, type AppNode } from "../nodes";

const text = (value: unknown) =>
  typeof value === "string" ? value.trim() : "";

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
