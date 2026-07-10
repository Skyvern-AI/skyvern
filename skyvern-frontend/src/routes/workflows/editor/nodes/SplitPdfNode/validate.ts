import type { SplitPdfNode } from "./types";

export function validateSplitPdfNode(node: SplitPdfNode): Array<string> {
  const errors: Array<string> = [];
  if (!node.data.fileUrl.trim()) {
    errors.push(`${node.data.label}: File URL is required.`);
  }
  if (!node.data.prompt.trim()) {
    errors.push(`${node.data.label}: Prompt is required.`);
  }
  return errors;
}
