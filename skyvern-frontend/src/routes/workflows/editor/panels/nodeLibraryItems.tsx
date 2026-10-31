import { WorkflowBlockTypes } from "../../types/workflowTypes";
import type { WorkflowBlockNode } from "../nodes";
import { WorkflowBlockIcon } from "../nodes/WorkflowBlockIcon";
import type { IntegrationCatalogEntry } from "@/hooks/useIntegrationCatalogQuery";

export const enableCodeBlock =
  import.meta.env.VITE_ENABLE_CODE_BLOCK?.toLowerCase() === "true";

export const nodeLibraryItems: Array<{
  nodeType: NonNullable<WorkflowBlockNode["type"]>;
  icon: JSX.Element;
  title: string;
  description: string;
}> = [
  {
    nodeType: "login",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.Login}
        className="size-6"
      />
    ),
    title: "Login Block",
    description: "Login to a website",
  },
  {
    nodeType: "navigation",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.Navigation}
        className="size-6"
      />
    ),
    title: "Browser Task Block",
    description: "Take actions to achieve a task.",
  },
  {
    nodeType: "action",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.Action}
        className="size-6"
      />
    ),
    title: "Browser Action Block",
    description: "Take a single action",
  },
  {
    nodeType: "extraction",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.Extraction}
        className="size-6"
      />
    ),
    title: "Extraction Block",
    description: "Extract data from a webpage",
  },
  {
    nodeType: "validation",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.Validation}
        className="size-6"
      />
    ),
    title: "AI Validation Block",
    description: "Have an AI validate the state of the screen",
  },
  {
    nodeType: "human_interaction",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.HumanInteraction}
        className="size-6"
      />
    ),
    title: "Human Interaction Block",
    description: "Pause agent for human review and approval",
  },
  // Data export now lives inside the Extraction block's "Export" section
  // (SKY-15396). Existing workflows that already have a Data Export node keep
  // working -- this only hides it from new-block authoring.
  // {
  //   nodeType: "dataExport",
  //   icon: (
  //     <WorkflowBlockIcon
  //       workflowBlockType={WorkflowBlockTypes.DataExport}
  //       className="size-6"
  //     />
  //   ),
  //   title: "Data Export Block",
  //   description: "Write schema-defined records to a Parquet file",
  // },
  // {
  //   nodeType: "task",
  //   icon: (
  //     <WorkflowBlockIcon
  //       workflowBlockType={WorkflowBlockTypes.Task}
  //       className="size-6"
  //     />
  //   ),
  //   title: "Task Block",
  //   description: "Complete multi-step browser automation tasks",
  // },
  {
    nodeType: "url",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.URL}
        className="size-6"
      />
    ),
    title: "Go to URL Block",
    description: "Navigate to a specific URL",
  },
  {
    nodeType: "textPrompt",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.TextPrompt}
        className="size-6"
      />
    ),
    title: "Text Prompt Block",
    description: "Process text with LLM",
  },
  {
    nodeType: "conditional",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.Conditional}
        className="size-6"
      />
    ),
    title: "Conditional Block",
    description: "Branch execution based on conditions",
  },
  {
    nodeType: "sendEmail",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.SendEmail}
        className="size-6"
      />
    ),
    title: "Send Email Block",
    description: "Send email notifications",
  },
  {
    nodeType: "loop",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.ForLoop}
        className="size-6"
      />
    ),
    title: "Loop Block",
    description: "Iterate over a list or while a condition stays true",
  },
  {
    nodeType: "codeBlock",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.Code}
        className="size-6"
      />
    ),
    title: "Code Block",
    description: "Execute custom Python code",
  },
  {
    nodeType: "fileParser",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.FileURLParser}
        className="size-6"
      />
    ),
    title: "File Parser Block",
    description: "Parse PDFs, CSVs, Excel files, and Images",
  },
  // {
  //   nodeType: "pdfParser",
  //   icon: (
  //     <WorkflowBlockIcon
  //       workflowBlockType={WorkflowBlockTypes.PDFParser}
  //       className="size-6"
  //     />
  //   ),
  //   title: "PDF Parser Block",
  //   description: "Extract data from PDF files",
  // },
  //   nodeType: "upload",
  //   icon: (
  //     <WorkflowBlockIcon
  //       workflowBlockType={WorkflowBlockTypes.UploadToS3}
  //       className="size-6"
  //     />
  //   ),
  //   title: "Upload to S3 Block",
  //   description: "Upload files to AWS S3",
  // },
  // {
  //   nodeType: "download",
  //   icon: (
  //     <WorkflowBlockIcon
  //       workflowBlockType={WorkflowBlockTypes.DownloadToS3}
  //       className="size-6"
  //     />
  //   ),
  //   title: "Download from S3 Block",
  //   description: "Download files from AWS S3",
  // },
  {
    nodeType: "fileUpload",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.FileUpload}
        className="size-6"
      />
    ),
    title: "Cloud Storage Block",
    description: "Upload files to storage",
  },
  {
    nodeType: "fileDownload",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.FileDownload}
        className="size-6"
      />
    ),
    title: "File Download Block",
    description: "Download files from a website",
  },
  {
    nodeType: "wait",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.Wait}
        className="size-6"
      />
    ),
    title: "Wait Block",
    description: "Wait for a specified amount of time",
  },
  {
    nodeType: "terminate",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.Terminate}
        className="size-6"
      />
    ),
    title: "Terminate Block",
    description: "End the run as terminated with a reason",
  },
  {
    nodeType: "agentStep",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.AgentStep}
        className="size-6"
      />
    ),
    title: "Agent",
    description: "Call this run's blocks as tools until it can answer",
  },
  {
    nodeType: "web_search",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.WebSearch}
        className="size-6"
      />
    ),
    title: "Web Search Block",
    description: "Search Google or Exa, with an optional prompt",
  },
  {
    nodeType: "http_request",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.HttpRequest}
        className="size-6"
      />
    ),
    title: "HTTP Request Block",
    description: "Make HTTP API calls",
  },
  {
    nodeType: "printPage",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.PrintPage}
        className="size-6"
      />
    ),
    title: "Print Page Block",
    description: "Print current page to PDF",
  },
  {
    nodeType: "pdfFill",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.PDFFill}
        className="size-6"
      />
    ),
    title: "PDF Fill Block",
    description: "Fill a PDF form",
  },
  {
    nodeType: "splitPdf",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.SplitPDF}
        className="size-6"
      />
    ),
    title: "Split PDF",
    description:
      "Split one PDF into multiple PDFs based on a prompt and save each to S3.",
  },
  {
    nodeType: "workflowTrigger",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.WorkflowTrigger}
        className="size-6"
      />
    ),
    title: "Agent Trigger Block",
    description: "Trigger another agent",
  },
  {
    nodeType: "emailInbox",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.EmailInbox}
        className="size-6"
      />
    ),
    title: "Email Inbox",
    description: "Search Gmail or Outlook inboxes",
  },
  {
    nodeType: "googleSheetsRead",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.GoogleSheetsRead}
        className="size-6"
      />
    ),
    title: "Google Sheets Read",
    description: "Read data from a Google Sheet",
  },
  {
    nodeType: "powerBIQuery",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.PowerBIQuery}
        className="size-6"
      />
    ),
    title: "Power BI Query",
    description: "Run a read-only DAX query against a dataset",
  },
  {
    nodeType: "googleSheetsWrite",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.GoogleSheetsWrite}
        className="size-6"
      />
    ),
    title: "Google Sheets Write",
    description: "Write data to a Google Sheet",
  },
];

export function availableNodeLibraryItems({
  agentBlockEnabled = false,
  integrationCatalog,
}: {
  agentBlockEnabled?: boolean;
  integrationCatalog?: readonly IntegrationCatalogEntry[];
} = {}) {
  return nodeLibraryItems.filter(
    (item) =>
      (enableCodeBlock || item.nodeType !== "codeBlock") &&
      (agentBlockEnabled || item.nodeType !== "agentStep") &&
      (item.nodeType !== "powerBIQuery" ||
        integrationCatalog?.some(
          (entry) => entry.id === "powerbi" && entry.status === "live",
        )),
  );
}
