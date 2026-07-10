import type { LoopNodeData } from "./LoopNode/types";

// `loop` is intentionally absent — it is disambiguated to for_loop / while_loop
// below based on the node's loopKind data field.
const REACT_FLOW_TYPE_TO_BLOCK_TYPE: Record<string, string> = {
  task: "task",
  conditional: "conditional",
  textPrompt: "text_prompt",
  sendEmail: "send_email",
  codeBlock: "code",
  fileParser: "file_url_parser",
  upload: "upload_to_s3",
  fileUpload: "file_upload",
  download: "download_to_s3",
  validation: "validation",
  human_interaction: "human_interaction",
  action: "action",
  navigation: "navigation",
  extraction: "extraction",
  login: "login",
  wait: "wait",
  fileDownload: "file_download",
  pdfParser: "pdf_parser",
  taskv2: "task_v2",
  url: "goto_url",
  http_request: "http_request",
  printPage: "print_page",
  workflowTrigger: "workflow_trigger",
  googleSheetsRead: "google_sheets_read",
  googleSheetsWrite: "google_sheets_write",
  pdfFill: "pdf_fill",
  splitPdf: "split_pdf",
};

type Loopish = { type?: string; data?: Partial<LoopNodeData> };

// Returns null when the React Flow type has no mapping so callers can decide a
// safe fallback. Returning the raw camelCase type would silently break
// snake_case-only PostHog funnels when a new node type is added.
export function blockTypeFromNode(node: Loopish): string | null {
  const reactFlowType = node?.type;
  if (!reactFlowType) return null;
  if (reactFlowType === "loop") {
    return node.data?.loopKind === "while" ? "while_loop" : "for_loop";
  }
  return REACT_FLOW_TYPE_TO_BLOCK_TYPE[reactFlowType] ?? null;
}
