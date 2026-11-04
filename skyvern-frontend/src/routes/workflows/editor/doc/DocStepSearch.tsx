import { useMemo } from "react";
import {
  CommandDialog,
  CommandEmpty,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { WorkflowBlockIcon } from "../nodes/WorkflowBlockIcon";
import { isWorkflowBlockNode, type AppNode } from "../nodes";
import { workflowBlockTitle } from "../nodes/types";
import {
  docBlockType,
  docStepProducesData,
  getDocProseFields,
} from "./docStepRegistry";
import { docOutlineEntries } from "./docOutlineEntries";
import type { DocItem } from "./buildDocTree";

type SearchEntry = {
  id: string;
  title: string;
  type: string;
  output: string | null;
  keywords: string;
};

function text(value: unknown) {
  return typeof value === "string" ? value : "";
}

function searchEntries(items: DocItem[], byId: Map<string, AppNode>) {
  return docOutlineEntries(items, byId).flatMap((entry): SearchEntry[] => {
    const node = byId.get(entry.id);
    const blockType =
      node && isWorkflowBlockNode(node) ? docBlockType(node) : null;
    if (!node || !blockType) return [];
    const data = node.data as Record<string, unknown>;
    const outputKey = `${node.data.label}_output`;
    const output = docStepProducesData(node) ? outputKey : null;
    const keywords = [
      node.data.label,
      text(data.description),
      ...getDocProseFields(node).map((field) => text(data[field.key])),
      outputKey,
    ].filter(Boolean);
    return [
      {
        id: entry.id,
        title: entry.title,
        type: workflowBlockTitle[blockType],
        output,
        keywords: keywords.join(" "),
      },
    ];
  });
}

export function DocStepSearch({
  open,
  onOpenChange,
  items,
  byId,
  goTo,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  items: DocItem[];
  byId: Map<string, AppNode>;
  goTo: (id: string) => void;
}) {
  const entries = useMemo(() => searchEntries(items, byId), [items, byId]);
  return (
    <CommandDialog open={open} onOpenChange={onOpenChange}>
      <DialogTitle className="sr-only">Search steps</DialogTitle>
      <DialogDescription className="sr-only">
        Search and jump to a workflow step.
      </DialogDescription>
      <CommandInput autoFocus placeholder="Search steps…" />
      <CommandList>
        <CommandEmpty>No steps match.</CommandEmpty>
        {entries.map((entry) => {
          const node = byId.get(entry.id)!;
          const blockType = docBlockType(node)!;
          return (
            <CommandItem
              key={entry.id}
              value={`${entry.title} ${entry.type} ${entry.keywords}`}
              onSelect={() => {
                onOpenChange(false);
                goTo(entry.id);
              }}
              className="gap-2.5"
            >
              <span className="grid size-7 shrink-0 place-items-center rounded-md border border-border">
                <WorkflowBlockIcon
                  workflowBlockType={blockType}
                  className="size-4"
                />
              </span>
              <span className="min-w-0">
                <span className="block truncate text-sm leading-tight">
                  {entry.title}
                </span>
                <span className="block truncate text-xs leading-tight text-muted-foreground">
                  {entry.type}
                  {entry.output ? ` · ${entry.output}` : ""}
                </span>
              </span>
            </CommandItem>
          );
        })}
      </CommandList>
    </CommandDialog>
  );
}
