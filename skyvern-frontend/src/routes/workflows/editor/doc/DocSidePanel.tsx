import { useEffect, useRef } from "react";
import { Cross2Icon } from "@radix-ui/react-icons";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { BlockConfigForm } from "../panels/BlockConfigForm";
import { isWorkflowBlockNode, type AppNode } from "../nodes";
import { blockTypeFromNode } from "../nodes/blockTypeFromNode";
import { WorkflowBlockIcon } from "../nodes/WorkflowBlockIcon";
import { workflowBlockTitle } from "../nodes/types";
import type { WorkflowBlockType } from "../../types/workflowTypes";
import { docLearnContent, type DocLearnContent } from "./docLearnContent";

export function DocSidePanel({
  tab,
  onTabChange,
  selectedStep,
  editable,
  learn,
  onLearn,
  onClose,
}: {
  tab: string;
  onTabChange: (tab: string) => void;
  selectedStep?: AppNode;
  editable: boolean;
  learn: DocLearnContent;
  onLearn: (content: DocLearnContent) => void;
  onClose: () => void;
}) {
  const selectedStepId = selectedStep?.id;
  const blockType = (
    selectedStep && isWorkflowBlockNode(selectedStep)
      ? selectedStep.type === "dataExport"
        ? "data_export"
        : blockTypeFromNode(selectedStep)
      : null
  ) as WorkflowBlockType | null;
  const panelRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const frame = requestAnimationFrame(() =>
      panelRef.current
        ?.querySelector<HTMLButtonElement>('[role="tab"][data-state="active"]')
        ?.focus(),
    );
    return () => cancelAnimationFrame(frame);
  }, [selectedStepId, learn.id]);
  return (
    <aside
      ref={panelRef}
      aria-label="Document panel"
      className="absolute inset-0 z-20 overflow-y-auto bg-slate-elevation1 sm:relative sm:inset-auto sm:w-96 sm:max-w-[45%] sm:shrink-0 sm:border-l sm:border-border"
    >
      <button
        type="button"
        aria-label="Close document panel"
        onClick={onClose}
        className="absolute right-3 top-3 z-10 rounded p-1.5 text-muted-foreground outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
      >
        <Cross2Icon className="size-4" />
      </button>
      <Tabs value={tab} onValueChange={onTabChange} className="p-4">
        <TabsList className="mb-4 mr-10">
          <TabsTrigger value="settings">Settings</TabsTrigger>
          <TabsTrigger value="learn">Learn</TabsTrigger>
        </TabsList>
        <TabsContent value="settings">
          {selectedStep && (
            <div
              className="mb-4 flex min-w-0 items-center gap-2 text-sm"
              aria-label="Selected step"
            >
              {blockType && (
                <WorkflowBlockIcon
                  workflowBlockType={blockType}
                  className="size-4 shrink-0"
                />
              )}
              <span className="shrink-0">
                {blockType ? workflowBlockTitle[blockType] : selectedStep.type}
              </span>
              <span
                className="min-w-0 truncate font-mono text-xs text-muted-foreground"
                title={selectedStep.data.label}
              >
                {selectedStep.data.label}
              </span>
            </div>
          )}
          {selectedStepId && editable ? (
            <BlockConfigForm key={selectedStepId} blockId={selectedStepId} />
          ) : (
            <p className="text-sm text-muted-foreground">
              {selectedStepId
                ? "Open Visual to inspect this step’s settings."
                : "Choose a step to edit its settings."}
            </p>
          )}
        </TabsContent>
        <TabsContent value="learn">
          <h2 className="mb-3 text-lg font-semibold">{learn.title}</h2>
          <p className="whitespace-pre-wrap text-sm leading-6 text-muted-foreground">
            {learn.description}
          </p>
          <div className="mt-6 space-y-2">
            {docLearnContent
              .filter((content) => content.id !== learn.id)
              .map((content) => (
                <button
                  key={content.id}
                  type="button"
                  onClick={() => onLearn(content)}
                  className="block w-full rounded border-b border-border py-2 text-left text-sm outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
                >
                  {content.title}
                </button>
              ))}
          </div>
        </TabsContent>
      </Tabs>
    </aside>
  );
}
