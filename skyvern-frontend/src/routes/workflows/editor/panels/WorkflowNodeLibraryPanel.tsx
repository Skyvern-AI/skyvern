import { ScrollArea, ScrollAreaViewport } from "@/components/ui/scroll-area";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { useState, useRef, useEffect } from "react";
import {
  Cross2Icon,
  PlusIcon,
  MagnifyingGlassIcon,
} from "@radix-ui/react-icons";
import { WorkflowBlockTypes } from "../../types/workflowTypes";
import { WorkflowBlockNode } from "../nodes";
import { WorkflowBlockIcon } from "../nodes/WorkflowBlockIcon";
import { AddNodeProps } from "../Workspace";
import { Input } from "@/components/ui/input";
import { useNodes } from "@xyflow/react";
import { AppNode } from "../nodes";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

const enableCodeBlock =
  import.meta.env.VITE_ENABLE_CODE_BLOCK?.toLowerCase() === "true";

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
    title: "Browser Task Block",
    description: "Take actions to achieve a task.",
  },
  {
    nodeType: "taskv2",
    icon: (
      <WorkflowBlockIcon
        workflowBlockType={WorkflowBlockTypes.Taskv2}
        className="size-6"
      />
    ),
    title: "Browser Task v2 Block",
    description: "Achieve complex tasks with deep thinking.",
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
    title: "AI or Human Validation",
    description: "Have an AI or Human validate the state of the screen",
  },
  /**
   * The Human Interaction block can be had via a transmutation of the
   * Validation block.
   */
  // {
  //   nodeType: "human_interaction",
  //   icon: (
  //     <WorkflowBlockIcon
  //       workflowBlockType={WorkflowBlockTypes.HumanInteraction}
  //       className="size-6"
  //     />
  //   ),
  //   title: "Human Interaction Block",
  //   description: "Validate via human interaction",
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
    description: "Repeat blocks for each item",
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
    description: "Parse PDFs, CSVs, and Excel files",
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
];

type Props = {
  onMouseDownCapture?: () => void;
  onNodeClick: (props: AddNodeProps) => void;
  first?: boolean;
};

function WorkflowNodeLibraryPanel({
  onMouseDownCapture,
  onNodeClick,
  first,
}: Props) {
  const nodes = useNodes() as Array<AppNode>;
  const workflowPanelData = useWorkflowPanelStore(
    (state) => state.workflowPanelState.data,
  );
  const workflowPanelActive = useWorkflowPanelStore(
    (state) => state.workflowPanelState.active,
  );
  const closeWorkflowPanel = useWorkflowPanelStore(
    (state) => state.closeWorkflowPanel,
  );
  const [search, setSearch] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  // Determine parent context to check if certain blocks should be disabled
  const parentNode = workflowPanelData?.parent
    ? nodes.find((n) => n.id === workflowPanelData.parent)
    : null;
  const parentType = parentNode?.type;

  // Check if a node type should be disabled based on parent context
  const isBlockDisabled = (
    nodeType: NonNullable<WorkflowBlockNode["type"]>,
  ): { disabled: boolean; reason: string } => {
    // Disable conditional inside conditional
    if (nodeType === "conditional" && parentType === "conditional") {
      return {
        disabled: true,
        reason:
          "We're working on supporting nested conditionals. Soon you'll be able to use this feature!",
      };
    }
    return { disabled: false, reason: "" };
  };

  useEffect(() => {
    // Focus the input when the panel becomes active
    if (workflowPanelActive && inputRef.current) {
      // Use multiple approaches to ensure focus works
      const focusInput = () => {
        if (inputRef.current) {
          inputRef.current.focus();
          inputRef.current.select(); // Also select any existing text
        }
      };

      // Try immediate focus
      focusInput();

      // Also try with a small delay for animations/transitions
      const timeoutId = setTimeout(() => {
        focusInput();
      }, 100);

      // And try with a longer delay as backup
      const backupTimeoutId = setTimeout(() => {
        focusInput();
      }, 300);

      return () => {
        clearTimeout(timeoutId);
        clearTimeout(backupTimeoutId);
      };
    }
  }, [workflowPanelActive]);

  const filteredItems = nodeLibraryItems.filter((item) => {
    if (workflowPanelData?.disableLoop && item.nodeType === "loop") {
      return false;
    }
    if (!enableCodeBlock && item.nodeType === "codeBlock") {
      return false;
    }

    const term = search.toLowerCase();
    if (!term) {
      return true;
    }

    return (
      item.nodeType.toLowerCase().includes(term) ||
      item.title.toLowerCase().includes(term)
    );
  });

  return (
    <div
      className="h-full w-[25rem] rounded-xl border border-slate-700 bg-slate-950 p-5 shadow-xl"
      onMouseDownCapture={() => onMouseDownCapture?.()}
    >
      <div className="flex h-full flex-col space-y-4">
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
              ? "Click on the block type to add your first block"
              : "Click on the block type you want to add"}
          </span>
        </header>
        <div className="relative">
          <div className="absolute left-0 top-0 flex size-9 items-center justify-center">
            <MagnifyingGlassIcon className="size-5" />
          </div>
          <Input
            value={search}
            onChange={(event) => {
              setSearch(event.target.value);
            }}
            placeholder="Search blocks..."
            className="pl-9"
            ref={inputRef}
            autoFocus
            tabIndex={0}
          />
        </div>
        <ScrollArea className="h-full flex-1">
          <ScrollAreaViewport className="h-full">
            <div className="space-y-2">
              {filteredItems.length > 0 ? (
                filteredItems.map((item) => {
                  const { disabled, reason } = isBlockDisabled(item.nodeType);
                  const itemContent = (
                    <div
                      key={item.nodeType}
                      className={`flex items-center justify-between rounded-sm bg-slate-elevation4 p-4 ${
                        disabled
                          ? "cursor-not-allowed opacity-50"
                          : "cursor-pointer hover:bg-slate-elevation5"
                      }`}
                      onClick={() => {
                        if (disabled) return;
                        onNodeClick({
                          nodeType: item.nodeType,
                          next: workflowPanelData?.next ?? null,
                          parent: workflowPanelData?.parent,
                          previous: workflowPanelData?.previous ?? null,
                          connectingEdgeType:
                            workflowPanelData?.connectingEdgeType ??
                            "edgeWithAddButton",
                          branch: workflowPanelData?.branchContext,
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

                  // Wrap with tooltip if disabled
                  if (disabled) {
                    return (
                      <TooltipProvider key={item.nodeType}>
                        <Tooltip>
                          <TooltipTrigger asChild>{itemContent}</TooltipTrigger>
                          <TooltipContent side="right">
                            <p className="max-w-xs">{reason}</p>
                          </TooltipContent>
                        </Tooltip>
                      </TooltipProvider>
                    );
                  }

                  return itemContent;
                })
              ) : (
                <div className="p-4 text-center text-sm text-slate-400">
                  No results found
                </div>
              )}
            </div>
          </ScrollAreaViewport>
        </ScrollArea>
      </div>
    </div>
  );
}

export { WorkflowNodeLibraryPanel };
