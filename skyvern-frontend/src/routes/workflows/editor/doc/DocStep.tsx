import { useContext, type ReactNode } from "react";
import { ChevronRightIcon, GearIcon } from "@radix-ui/react-icons";
import { cn } from "@/util/utils";
import type { WorkflowBlockNode } from "../nodes";
import { WorkflowBlockIcon } from "../nodes/WorkflowBlockIcon";
import { workflowBlockTitle } from "../nodes/types";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";
import { getDocSettingsChips } from "./docSettingsChips";
import {
  docBlockType,
  docTagClasses,
  docTagColor,
  getDocKeyFields,
  getDocProseFields,
  getDocStepSummary,
} from "./docStepRegistry";
import { DocText } from "./DocText";

export function DocTag({ node }: { node: WorkflowBlockNode }) {
  const blockType = docBlockType(node);
  return (
    <span
      className={cn(
        "inline-flex h-[21px] shrink-0 items-center gap-1 whitespace-nowrap rounded pl-[5px] pr-[7px] text-[12.5px] font-semibold",
        docTagClasses[docTagColor(blockType)],
      )}
    >
      {blockType && (
        <WorkflowBlockIcon workflowBlockType={blockType} className="size-3" />
      )}
      {blockType ? workflowBlockTitle[blockType] : node.type}
    </span>
  );
}

export function DocSettingsButton({
  node,
  editable,
}: {
  node: WorkflowBlockNode;
  editable: boolean;
}) {
  const actions = useContext(WorkflowDocActionsContext);
  if (!editable) return null;
  const changed = getDocSettingsChips(node).length;
  return (
    <button
      type="button"
      aria-label={`Settings for ${node.data.label}${changed ? `, ${changed} changed` : ""}`}
      title={
        changed
          ? `${changed} setting${changed > 1 ? "s" : ""} changed`
          : "Settings"
      }
      onClick={() => actions.openSettings(node.id)}
      className={cn(
        "inline-flex items-center gap-[3px] rounded-[5px] py-0.5 text-xs font-semibold leading-4 text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-ring",
        changed
          ? "bg-muted pl-1 pr-1.5 text-foreground"
          : "px-1 opacity-0 group-focus-within/line:opacity-100 group-hover/line:opacity-100",
      )}
    >
      <GearIcon className="size-[15px]" />
      {changed > 0 && <span>{changed}</span>}
    </button>
  );
}

export function DocStepHeader({
  node,
  editable,
  extra,
  disclosure,
}: {
  node: WorkflowBlockNode;
  editable: boolean;
  extra?: ReactNode;
  disclosure?: { open: boolean; onToggle: () => void };
}) {
  return (
    <div className="relative flex min-h-[26px] flex-wrap items-center gap-2">
      {disclosure && (
        <button
          type="button"
          aria-expanded={disclosure.open}
          aria-label={`${disclosure.open ? "Collapse" : "Expand"} ${node.data.label}`}
          onClick={disclosure.onToggle}
          className="absolute -left-[26px] top-0.5 grid h-[22px] w-[22px] place-items-center rounded text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
        >
          <ChevronRightIcon
            className={cn(
              "size-4 transition-transform",
              disclosure.open && "rotate-90",
            )}
          />
        </button>
      )}
      <DocTag node={node} />
      {extra}
      {node.type !== "conditional" && (
        <span className="min-w-0 text-[12.5px] text-muted-foreground [overflow-wrap:anywhere]">
          saves{" "}
          <span
            title={`${node.data.label}_output`}
            className="inline-block max-w-full rounded bg-muted px-[5px] font-medium leading-[1.25] [overflow-wrap:anywhere]"
          >
            {node.data.label}_output
          </span>
        </span>
      )}
      <DocSettingsButton node={node} editable={editable} />
    </div>
  );
}

export function DocStepBody({ node }: { node: WorkflowBlockNode }) {
  const data = node.data as Record<string, unknown>;
  const prose = getDocProseFields(node).flatMap((f) =>
    typeof data[f.key] === "string" && (data[f.key] as string).trim()
      ? [{ ...f, value: data[f.key] as string }]
      : [],
  );
  const description =
    typeof node.data.description === "string" && node.data.description.trim()
      ? node.data.description
      : null;
  const isProse = getDocProseFields(node).length > 0;
  const [main, ...rest] = prose;
  const keyFields = isProse ? [] : getDocKeyFields(node);
  return (
    <>
      <p className="min-h-[1.55em] text-base font-medium leading-[1.55]">
        {isProse ? (
          main ? (
            <DocText value={main.value} />
          ) : (
            <span className="font-normal text-muted-foreground">
              No instructions yet
            </span>
          )
        ) : (
          <DocText value={description ?? getDocStepSummary(node)} />
        )}
      </p>
      {rest.map((f) => (
        <p
          key={f.key}
          className="text-[13.5px] leading-[1.55] text-muted-foreground"
        >
          <span className="mr-1.5">{f.label}</span>
          <DocText value={f.value} className="text-foreground" />
        </p>
      ))}
      {keyFields.length > 0 && (
        <div className="mb-0.5 mt-1 flex flex-wrap gap-1.5">
          {keyFields.map((f) => (
            <span
              key={f.key}
              className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-border px-2 py-px text-[12.5px]"
            >
              <span className="shrink-0 text-muted-foreground">{f.label}</span>
              <DocText value={f.value} className="min-w-0" />
            </span>
          ))}
        </div>
      )}
    </>
  );
}
