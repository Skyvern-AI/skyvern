import { ScrollArea, ScrollAreaViewport } from "@/components/ui/scroll-area";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { useState, useRef, useEffect } from "react";
import { PlusIcon, MagnifyingGlassIcon } from "@radix-ui/react-icons";
import { AddNodeProps } from "../Workspace";
import { Input } from "@/components/ui/input";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { AGENT_BLOCK_FLAG } from "@/util/featureFlags";
import { availableNodeLibraryItems } from "./nodeLibraryItems";
import { useIntegrationCatalogQuery } from "@/hooks/useIntegrationCatalogQuery";

type Props = {
  onNodeClick: (props: AddNodeProps) => void;
};

function WorkflowNodeLibraryPanel({ onNodeClick }: Props) {
  const workflowPanelData = useWorkflowPanelStore(
    (state) => state.workflowPanelState.data,
  );
  const workflowPanelActive = useWorkflowPanelStore(
    (state) => state.workflowPanelState.active,
  );
  const closeWorkflowPanel = useWorkflowPanelStore(
    (state) => state.closeWorkflowPanel,
  );
  const enableAgentBlock = useFeatureFlag(AGENT_BLOCK_FLAG) === true;
  const { data: integrationCatalog } = useIntegrationCatalogQuery();
  const [search, setSearch] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

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

  const filteredItems = availableNodeLibraryItems({
    agentBlockEnabled: enableAgentBlock,
    integrationCatalog,
  }).filter((item) => {
    if (workflowPanelData?.disableLoop && item.nodeType === "loop") {
      return false;
    }

    const term = search.toLowerCase();
    if (!term) {
      return true;
    }

    return (
      item.nodeType.toLowerCase().includes(term) ||
      item.title.toLowerCase().includes(term) ||
      item.description.toLowerCase().includes(term)
    );
  });

  return (
    <div className="flex h-full min-h-0 w-full min-w-0 flex-col gap-4">
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
      <ScrollArea className="min-h-0 min-w-0 flex-1">
        {/* Radix wraps viewport children in a display:table div by default;
        override it so narrow sidebar rows can shrink instead of widening the
        clipped editor shell. */}
        <ScrollAreaViewport className="h-full w-full [&>div]:!block [&>div]:!overflow-x-hidden">
          <div className="space-y-2">
            {filteredItems.length > 0 ? (
              filteredItems.map((item) => (
                <div
                  key={item.nodeType}
                  data-testid={`block-library-item-${item.nodeType}`}
                  className="flex min-w-0 cursor-pointer items-center justify-between rounded-sm bg-slate-elevation4 p-4 hover:bg-slate-elevation5"
                  onClick={() => {
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
                  <div className="flex min-w-0 gap-2">
                    <div className="flex h-[2.75rem] w-[2.75rem] shrink-0 items-center justify-center rounded border border-border dark:border-slate-600">
                      {item.icon}
                    </div>
                    <div className="flex min-w-0 flex-col gap-1">
                      <span className="min-w-0 truncate text-base">
                        {item.title}
                      </span>
                      <span className="min-w-0 truncate text-xs text-slate-600 dark:text-slate-400">
                        {item.description}
                      </span>
                    </div>
                  </div>
                  <PlusIcon className="size-6 shrink-0" />
                </div>
              ))
            ) : (
              <div className="p-4 text-center text-sm text-muted-foreground">
                No results found
              </div>
            )}
          </div>
        </ScrollAreaViewport>
      </ScrollArea>
    </div>
  );
}

export { WorkflowNodeLibraryPanel };
