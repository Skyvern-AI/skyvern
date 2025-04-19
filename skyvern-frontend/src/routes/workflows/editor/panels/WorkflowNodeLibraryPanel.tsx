import { ScrollArea, ScrollAreaViewport } from "@/components/ui/scroll-area";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { Cross2Icon, PlusIcon } from "@radix-ui/react-icons";
import { WorkflowBlockTypes } from "../../types/workflowTypes";
import { AddNodeProps } from "../FlowRenderer";
import { WorkflowBlockNode } from "../nodes";
import { WorkflowBlockIcon } from "../nodes/WorkflowBlockIcon";

const enableCodeBlock = import.meta.env.VITE_ENABLE_CODE_BLOCK === "true";

const nodeLibraryItems: Array<{
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
    title: "Navigation Block",
    description: "Navigate on the page",
  },
  {
    nodeType: "taskv2",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.Taskv2}
        className="size-6"
      />
    ),
    title: "Navigation v2 Block",
    description: "Navigate on the page with Skyvern 2.0",
  },
  {
    nodeType: "action",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.Action}
        className="size-6"
      />
    ),
    title: "Action Block",
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
    description: "Extract data from the page",
  },
  {
    nodeType: "validation",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.Validation}
        className="size-6"
      />
    ),
    title: "Validation Block",
    description: "Validate the state of the workflow or terminate",
  },
  {
    nodeType: "task",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.Task}
        className="size-6"
      />
    ),
    title: "Task Block",
    description: "Takes actions or extracts information",
  },

  {
    nodeType: "url",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.URL}
        className="size-6"
      />
    ),
    title: "Go to URL Block",
    description: "Navigates to a URL",
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
    description: "Generates AI response",
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
    description: "Sends an email",
  },
  {
    nodeType: "loop",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.ForLoop}
        className="size-6"
      />
    ),
    title: "For Loop Block",
    description: "Repeats nested elements",
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
    description: "Executes Python code",
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
    description: "Downloads and parses a file",
  },
  {
    nodeType: "pdfParser",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.PDFParser}
        className="size-6"
      />
    ),
    title: "PDF Parser Block",
    description: "Downloads and parses a PDF file with an optional data schema",
  },
  // disabled
  // {
  //   nodeType: "download",
  //   icon: (
  //     <WorkflowBlockIcon
  //       workflowBlockType={WorkflowBlockTypes.DownloadToS3}
  //       className="size-6"
  //     />
  //   ),
  //   title: "Download Block",
  //   description: "Downloads a file from S3",
  // },
  {
    nodeType: "fileUpload",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.FileUpload}
        className="size-6"
      />
    ),
    title: "File Upload Block",
    description: "Uploads downloaded files to where you want.",
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
    description: "Download a file",
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
    description: "Wait for some time",
  },
];

type Props = {
  onNodeClick: (props: AddNodeProps) => void;
  first?: boolean;
};

function WorkflowNodeLibraryPanel({ onNodeClick, first }: Props) {
  const workflowPanelData = useWorkflowPanelStore(
    (state) => state.workflowPanelState.data,
  );
  const closeWorkflowPanel = useWorkflowPanelStore(
    (state) => state.closeWorkflowPanel,
  );

  return (
    <div className="w-[25rem] rounded-xl border border-slate-700 bg-slate-950 p-5 shadow-xl">
      <div className="space-y-4">
        <header className="space-y-2">
          <div className="flex justify-between">
            <h1 className="text-lg">Block Library</h1>
            {!first && (
              <Cross2Icon
                className="size-6 cursor-pointer"
                onClick={() => {
                  closeWorkflowPanel();
                }}
              />
            )}
          </div>
          <span className="text-sm text-slate-400">
            {first
              ? "Click on the node type to add your first block"
              : "Click on the node type you want to add"}
          </span>
        </header>
        <ScrollArea>
          <ScrollAreaViewport className="max-h-[28rem]">
            <div className="space-y-2">
              {nodeLibraryItems.map((item) => {
                if (
                  workflowPanelData?.disableLoop &&
                  item.nodeType === "loop"
                ) {
                  return null;
                }
                if (!enableCodeBlock && item.nodeType === "codeBlock") {
                  return null;
                }
                return (
                  <div
                    key={item.nodeType}
                    className="flex cursor-pointer items-center justify-between rounded-sm bg-slate-elevation4 p-4 hover:bg-slate-elevation5"
                    onClick={() => {
                      onNodeClick({
                        nodeType: item.nodeType,
                        next: workflowPanelData?.next ?? null,
                        parent: workflowPanelData?.parent,
                        previous: workflowPanelData?.previous ?? null,
                        connectingEdgeType:
                          workflowPanelData?.connectingEdgeType ??
                          "edgeWithAddButton",
                      });
                      closeWorkflowPanel();
                    }}
                  >
                    <div className="flex gap-2">
                      <div className="flex h-[2.75rem] w-[2.75rem] shrink-0 items-center justify-center rounded border border-slate-600">
                        {item.icon}
                      </div>
                      <div className="flex flex-col gap-1">
                        <span className="max-w-64 truncate text-base">
                          {item.title}
                        </span>
                        <span className="text-xs text-slate-400">
                          {item.description}
                        </span>
                      </div>
                    </div>
                    <PlusIcon className="size-6 shrink-0" />
                  </div>
                );
              })}
            </div>
          </ScrollAreaViewport>
        </ScrollArea>
      </div>
    </div>
  );
}

export { WorkflowNodeLibraryPanel };
