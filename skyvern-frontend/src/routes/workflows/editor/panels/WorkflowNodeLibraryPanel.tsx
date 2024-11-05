import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import {
  Cross2Icon,
  CursorTextIcon,
  DownloadIcon,
  EnvelopeClosedIcon,
  FileIcon,
  ListBulletIcon,
  PlusIcon,
  UpdateIcon,
  UploadIcon,
} from "@radix-ui/react-icons";
import { WorkflowBlockNode } from "../nodes";
import { AddNodeProps } from "../FlowRenderer";

const nodeLibraryItems: Array<{
  nodeType: NonNullable<WorkflowBlockNode["type"]>;
  icon: JSX.Element;
  title: string;
  description: string;
}> = [
  {
    nodeType: "task",
    icon: <ListBulletIcon className="h-6 w-6" />,
    title: "Task Block",
    description: "Takes actions or extracts information",
  },
  {
    nodeType: "loop",
    icon: <UpdateIcon className="h-6 w-6" />,
    title: "For Loop Block",
    description: "Repeats nested elements",
  },
  {
    nodeType: "textPrompt",
    icon: <CursorTextIcon className="h-6 w-6" />,
    title: "Text Prompt Block",
    description: "Generates AI response",
  },
  {
    nodeType: "sendEmail",
    icon: <EnvelopeClosedIcon className="h-6 w-6" />,
    title: "Send Email Block",
    description: "Sends an email",
  },
  // temporarily removed
  // {
  //   nodeType: "codeBlock",
  //   icon: <CodeIcon className="h-6 w-6" />,
  //   title: "Code Block",
  //   description: "Executes Python code",
  // },
  {
    nodeType: "fileParser",
    icon: <FileIcon className="h-6 w-6" />,
    title: "File Parser Block",
    description: "Downloads and parses a file",
  },
  {
    nodeType: "download",
    icon: <DownloadIcon className="h-6 w-6" />,
    title: "Download Block",
    description: "Downloads a file from S3",
  },
  {
    nodeType: "upload",
    icon: <UploadIcon className="h-6 w-6" />,
    title: "Upload Block",
    description: "Uploads a file to S3",
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
                className="h-6 w-6 cursor-pointer"
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
        <div className="space-y-2">
          {nodeLibraryItems.map((item) => {
            if (workflowPanelData?.disableLoop && item.nodeType === "loop") {
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
                <PlusIcon className="h-6 w-6" />
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

export { WorkflowNodeLibraryPanel };
