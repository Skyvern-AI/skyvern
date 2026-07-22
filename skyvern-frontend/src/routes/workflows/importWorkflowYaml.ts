import {
  parse as parseYAML,
  parseAllDocuments,
  stringify as convertToYAML,
} from "yaml";

function isJsonString(str: string): boolean {
  try {
    JSON.parse(str);
  } catch {
    return false;
  }
  return true;
}

// Bulk export bundles N workflows into one file: multi-document YAML (docs
// joined by `---`) or a top-level JSON array. Split it back into one YAML
// string per workflow so each can be POSTed as its own workflow. A file that
// holds a single workflow passes through unchanged.
export function expandFileToWorkflowYamls(text: string): string[] {
  if (isJsonString(text)) {
    const parsed = JSON.parse(text);
    if (Array.isArray(parsed)) {
      return parsed.map((workflow) => convertToYAML(workflow));
    }
    return [convertToYAML(parsed)];
  }
  const documents = parseAllDocuments(text);
  for (const document of documents) {
    // parseAllDocuments recovers from syntax errors instead of throwing, so
    // toJS() would hand back a silently-truncated object. Reject the file so it
    // falls back to raw-text passthrough and the backend rejects it, rather
    // than importing a partial workflow. An intentionally-empty trailing `---`
    // document has no errors and is dropped by the null filter below.
    const [error] = document.errors;
    if (error) {
      throw new Error(error.message);
    }
  }
  const workflows = documents
    .map((document) => document.toJS())
    .filter((value) => value !== null && typeof value === "object");
  if (workflows.length <= 1) {
    return [text];
  }
  return workflows.map((workflow) => convertToYAML(workflow));
}

export function extractTitleFromYaml(yaml: string): string | null {
  try {
    const parsed = parseYAML(yaml);
    if (parsed && typeof parsed === "object" && "title" in parsed) {
      const title = (parsed as { title?: unknown }).title;
      if (typeof title === "string" && title.trim().length > 0) {
        return title.trim();
      }
    }
  } catch {
    return null;
  }
  return null;
}
