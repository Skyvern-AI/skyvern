import { useNodesData } from "@xyflow/react";
import { useLayoutEffect, type ComponentType } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { AppNode, type WorkflowBlockNode } from "../nodes";
import { ActionBlockForm } from "./BlockConfigForm/ActionBlockForm";
import { CodeBlockBlockForm } from "./BlockConfigForm/CodeBlockBlockForm";
import { DownloadBlockForm } from "./BlockConfigForm/DownloadBlockForm";
import { ExtractionBlockForm } from "./BlockConfigForm/ExtractionBlockForm";
import { FileDownloadBlockForm } from "./BlockConfigForm/FileDownloadBlockForm";
import { FileParserBlockForm } from "./BlockConfigForm/FileParserBlockForm";
import { FileUploadBlockForm } from "./BlockConfigForm/FileUploadBlockForm";
import { GoogleSheetsReadBlockForm } from "./BlockConfigForm/GoogleSheetsReadBlockForm";
import { GoogleSheetsWriteBlockForm } from "./BlockConfigForm/GoogleSheetsWriteBlockForm";
import { HttpRequestBlockForm } from "./BlockConfigForm/HttpRequestBlockForm";
import { HumanInteractionBlockForm } from "./BlockConfigForm/HumanInteractionBlockForm";
import { LoginBlockForm } from "./BlockConfigForm/LoginBlockForm";
import { LoopBlockForm } from "./BlockConfigForm/LoopBlockForm";
import { NavigationBlockForm } from "./BlockConfigForm/NavigationBlockForm";
import { PDFParserBlockForm } from "./BlockConfigForm/PDFParserBlockForm";
import { PrintPageBlockForm } from "./BlockConfigForm/PrintPageBlockForm";
import { PdfFillBlockForm } from "./BlockConfigForm/PdfFillBlockForm";
import { SendEmailBlockForm } from "./BlockConfigForm/SendEmailBlockForm";
import { TaskBlockForm } from "./BlockConfigForm/TaskBlockForm";
import { Taskv2BlockForm } from "./BlockConfigForm/Taskv2BlockForm";
import { TextPromptBlockForm } from "./BlockConfigForm/TextPromptBlockForm";
import { URLBlockForm } from "./BlockConfigForm/URLBlockForm";
import { UploadBlockForm } from "./BlockConfigForm/UploadBlockForm";
import { ValidationBlockForm } from "./BlockConfigForm/ValidationBlockForm";
import { WaitBlockForm } from "./BlockConfigForm/WaitBlockForm";
import { WorkflowSettingsBlockForm } from "./BlockConfigForm/WorkflowSettingsBlockForm";
import { WorkflowTriggerBlockForm } from "./BlockConfigForm/WorkflowTriggerBlockForm";

type WorkflowBlockNodeType = WorkflowBlockNode["type"];

type BlockFormComponent = ComponentType<{ blockId: string }>;

// BranchesEditor runs auto-default-branch + auto-activeBranchId repair
// effects; mounting it in the sidebar would fire the same repairs from
// two concurrent instances. The canvas tile is the authoritative mount.
function ConditionalSidebarPlaceholder() {
  return (
    <div
      data-testid="block-config-form-conditional-placeholder"
      className="px-4 py-4 text-sm text-slate-400"
    >
      Edit conditional branches on the canvas tile.
    </div>
  );
}

const BLOCK_FORMS: Record<WorkflowBlockNodeType, BlockFormComponent> = {
  task: TaskBlockForm,
  taskv2: Taskv2BlockForm,
  navigation: NavigationBlockForm,
  extraction: ExtractionBlockForm,
  action: ActionBlockForm,
  login: LoginBlockForm,
  wait: WaitBlockForm,
  loop: LoopBlockForm,
  conditional: ConditionalSidebarPlaceholder,
  textPrompt: TextPromptBlockForm,
  sendEmail: SendEmailBlockForm,
  codeBlock: CodeBlockBlockForm,
  fileParser: FileParserBlockForm,
  fileDownload: FileDownloadBlockForm,
  download: DownloadBlockForm,
  upload: UploadBlockForm,
  fileUpload: FileUploadBlockForm,
  pdfParser: PDFParserBlockForm,
  validation: ValidationBlockForm,
  human_interaction: HumanInteractionBlockForm,
  url: URLBlockForm,
  http_request: HttpRequestBlockForm,
  printPage: PrintPageBlockForm,
  pdfFill: PdfFillBlockForm,
  workflowTrigger: WorkflowTriggerBlockForm,
  googleSheetsRead: GoogleSheetsReadBlockForm,
  googleSheetsWrite: GoogleSheetsWriteBlockForm,
};

function BlockConfigForm({ blockId }: Readonly<{ blockId: string }>) {
  const flushPendingCommit = usePendingCommitsStore((state) => state.flush);

  // useLayoutEffect cleanups run synchronously after commit and BEFORE
  // useEffect cleanups, so capturing `blockId` here flushes the previous
  // block's pending edits before the form's useEffect cleanup unregisters
  // the commit.
  useLayoutEffect(() => {
    return () => {
      flushPendingCommit(blockId);
    };
  }, [blockId, flushPendingCommit]);

  // Subscribe to the node's live slice so block transmutation (same id,
  // new type) re-dispatches to the matching form instead of writing
  // fields against a stale schema.
  const nodeSlice = useNodesData<AppNode>(blockId);
  if (!nodeSlice) {
    return null;
  }

  if (nodeSlice.type === "start") {
    return <WorkflowSettingsBlockForm blockId={blockId} />;
  }

  if (!(nodeSlice.type in BLOCK_FORMS)) {
    return null;
  }

  const Form = BLOCK_FORMS[nodeSlice.type as WorkflowBlockNode["type"]];
  return <Form blockId={blockId} />;
}

export {
  BLOCK_FORMS,
  BlockConfigForm,
  type BlockFormComponent,
  type WorkflowBlockNodeType,
};
