import type { WorkflowBlockType } from "@/routes/workflows/types/workflowTypes";

// Source-of-truth map: React Flow node-type key (matching `nodeTypes` in
// `../nodes/index.ts`) → backend `WorkflowBlockType` (snake_case). Adding a
// collapsible block type means adding one row here; the three exports
// below derive from this map so they cannot drift.
const COLLAPSIBLE_NODE_TYPE_TO_BLOCK_TYPE: Record<string, WorkflowBlockType> = {
  task: "task",
  navigation: "navigation",
  extraction: "extraction",
  textPrompt: "text_prompt",
  sendEmail: "send_email",
  codeBlock: "code",
  fileParser: "file_url_parser",
  upload: "upload_to_s3",
  fileUpload: "file_upload",
  download: "download_to_s3",
  validation: "validation",
  action: "action",
  human_interaction: "human_interaction",
  login: "login",
  wait: "wait",
  fileDownload: "file_download",
  pdfParser: "pdf_parser",
  taskv2: "task_v2",
  url: "goto_url",
  http_request: "http_request",
  printPage: "print_page",
  workflowTrigger: "workflow_trigger",
  emailInbox: "email_inbox",
  googleSheetsRead: "google_sheets_read",
  googleSheetsWrite: "google_sheets_write",
  pdfFill: "pdf_fill",
  splitPdf: "split_pdf",
  loop: "for_loop",
  conditional: "conditional",
};

// `loop` and `conditional` collapse via custom in-node implementations
// (hide children via `node.hidden`); the rest collapse via withCollapsible.
// Sentinels (`start`, `nodeAdder`) are excluded by virtue of not being in
// the source map.
export const collapsibleRfNodeTypes: ReadonlySet<string> = new Set<string>(
  Object.keys(COLLAPSIBLE_NODE_TYPE_TO_BLOCK_TYPE),
);

// NodeHeader receives `block_type` (not the RF node type) so the chevron
// gate consults this set rather than `collapsibleRfNodeTypes`.
// `while_loop` shares the `loop` RF node type but LoopNode passes
// "while_loop" as headerBlockType, so it must be in this set too.
export const collapsibleWorkflowBlockTypes: ReadonlySet<WorkflowBlockType> =
  new Set<WorkflowBlockType>([
    ...Object.values(COLLAPSIBLE_NODE_TYPE_TO_BLOCK_TYPE),
    "while_loop",
  ]);

export function toWorkflowBlockType(
  rfNodeType: string | undefined,
): WorkflowBlockType | null {
  if (rfNodeType === undefined) return null;
  return COLLAPSIBLE_NODE_TYPE_TO_BLOCK_TYPE[rfNodeType] ?? null;
}
