import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import {
  CheckCircledIcon,
  Cross2Icon,
  CursorTextIcon,
  EnvelopeClosedIcon,
  FileIcon,
  ListBulletIcon,
  PlusIcon,
  UpdateIcon,
  UploadIcon,
} from "@radix-ui/react-icons";
import { WorkflowBlockNode } from "../nodes";
import { AddNodeProps } from "../FlowRenderer";
import { ClickIcon } from "@/components/icons/ClickIcon";
import { ScrollArea, ScrollAreaViewport } from "@/components/ui/scroll-area";
import { RobotIcon } from "@/components/icons/RobotIcon";
import { ExtractIcon } from "@/components/icons/ExtractIcon";

const nodeLibraryItems: Array<{
  nodeType: NonNullable<WorkflowBlockNode["type"]>;
  icon: JSX.Element;
  title: string;
  description: string;
}> = [
  {
    nodeType: "task",
    icon: <ListBulletIcon className="size-6" />,
    title: "Task Block",
    description: "Takes actions or extracts information",
  },
  {
    nodeType: "loop",
    icon: <UpdateIcon className="size-6" />,
    title: "For Loop Block",
    description: "Repeats nested elements",
  },
  {
    nodeType: "textPrompt",
    icon: <CursorTextIcon className="size-6" />,
    title: "Text Prompt Block",
    description: "Generates AI response",
  },
  {
    nodeType: "sendEmail",
    icon: <EnvelopeClosedIcon className="size-6" />,
    title: "Send Email Block",
    description: "Sends an email",
  },
  // temporarily removed
  // {
  //   nodeType: "codeBlock",
  //   icon: <CodeIcon className="size-6" />,
  //   title: "Code Block",
  //   description: "Executes Python code",
  // },
  {
    nodeType: "fileParser",
    icon: <FileIcon className="size-6" />,
    title: "File Parser Block",
    description: "Downloads and parses a file",
  },
  // disabled
  // {
  //   nodeType: "download",
  //   icon: <DownloadIcon className="size-6" />,
  //   title: "Download Block",
  //   description: "Downloads a file from S3",
  // },
  {
    nodeType: "upload",
    icon: <UploadIcon className="size-6" />,
    title: "Upload Block",
    description: "Uploads a file to S3",
  },
  {
    nodeType: "validation",
    icon: <CheckCircledIcon className="size-6" />,
    title: "Validation Block",
    description: "Validate the state of the workflow or terminate",
  },
  {
    nodeType: "action",
    icon: <ClickIcon className="size-6" />,
    title: "Action Block",
    description: "Take a single action",
  },
  {
    nodeType: "navigation",
    icon: <RobotIcon className="size-6" />,
    title: "Navigation Block",
    description: "Navigate on the page",
  },
  {
    nodeType: "extraction",
    icon: <ExtractIcon className="size-6" />,
    title: "Extraction Block",
    description: "Extract data from the page",
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
            <h1 className="text-lg">Node Library</h1>
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
              ? "Click on the node type to add your first node"
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
                      <div className="flex h-[2.75rem] w-[2.75rem] items-center justify-center rounded border border-slate-600">
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
                    <PlusIcon className="size-6" />
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
