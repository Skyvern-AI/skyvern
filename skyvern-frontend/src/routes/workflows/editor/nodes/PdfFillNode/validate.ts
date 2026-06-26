import { validateJson } from "../HttpRequestNode/httpValidation";
import type { PdfFillNode } from "./types";

export function validatePdfFillNode(node: PdfFillNode): Array<string> {
  const errors: Array<string> = [];
  if (!node.data.fileUrl.trim()) {
    errors.push(`${node.data.label}: File URL is required.`);
  }
  if (!node.data.prompt.trim()) {
    errors.push(`${node.data.label}: Prompt is required.`);
  }
  // Payloads with any Jinja span are templated server-side before parsing, including inline
  // expressions for non-string values (e.g. {"items": {{ applicant_list }}}). Those aren't valid
  // JSON until rendered, so only JSON-validate JSON-shaped payloads that contain no {{ ... }} spans.
  const trimmedPayload = node.data.payload.trim();
  const containsJinja = /\{\{.*?\}\}/s.test(trimmedPayload);
  const looksLikeJson =
    trimmedPayload.startsWith("{") || trimmedPayload.startsWith("[");
  if (looksLikeJson && !containsJinja) {
    const payloadResult = validateJson(node.data.payload);
    if (!payloadResult.valid && payloadResult.message) {
      errors.push(`${node.data.label}: Payload - ${payloadResult.message}`);
    }
  }
  return errors;
}
