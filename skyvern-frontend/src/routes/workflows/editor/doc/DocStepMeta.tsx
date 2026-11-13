import { Fragment, useContext, type ComponentType } from "react";
import {
  CodeIcon,
  EnvelopeClosedIcon,
  ExclamationTriangleIcon,
  FileIcon,
  GlobeIcon,
  LockClosedIcon,
  MixIcon,
  Share1Icon,
  TableIcon,
  TextIcon,
} from "@radix-ui/react-icons";

import { cn } from "@/util/utils";
import type { WorkflowBlockNode } from "../nodes";
import type { DocMetaIcon } from "./docFieldMap";
import { docUrlHost, docUrlWithoutUserinfo } from "./docPromptUrl";
import { yamlKey, type DocSettingRow } from "./docSettingsChips";
import { docRequiredSettingKeys, docStepCaption } from "./docStepRegistry";
import { DocText } from "./DocText";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";

const ICONS: Record<DocMetaIcon, ComponentType<{ className?: string }>> = {
  credential: LockClosedIcon,
  url: GlobeIcon,
  file: FileIcon,
  email: EnvelopeClosedIcon,
  text: TextIcon,
  agent: Share1Icon,
  method: CodeIcon,
  range: TableIcon,
  model: MixIcon,
  errorCode: ExclamationTriangleIcon,
};

/** The line under a step's title: what it acts on or with, and a dashed chip for each such field still to fill. */
export function DocStepMeta({
  node,
  rows,
}: {
  node: WorkflowBlockNode;
  rows: readonly DocSettingRow[];
}) {
  const actions = useContext(WorkflowDocActionsContext);
  // The same answer the row split and the step's fix line use.
  const editable = actions.stepLinesEditable(node.id);
  if (!rows.length) return null;
  const name = docStepCaption(node) ?? node.data.label;
  const open = () => actions.openSettings(node.id);
  const required = docRequiredSettingKeys(node).map(yamlKey);
  return (
    <div
      data-doc-meta
      className="mb-0.5 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-[13.5px] leading-[1.55] text-muted-foreground"
    >
      {rows.map((row, i) => {
        const Icon = ICONS[row.meta!.icon];
        const icon = <Icon aria-hidden className="size-3.5 shrink-0" />;
        if (row.unset) {
          const needed = required.includes(row.key);
          return editable ? (
            <button
              key={row.key}
              type="button"
              data-doc-meta-add={row.key}
              aria-label={`Add ${row.addLabel ?? row.label} to ${name}${needed ? " (required)" : ""}`}
              onClick={open}
              className="inline-flex items-center gap-1.5 rounded-md border border-dashed border-border bg-background py-0.5 pl-[7px] pr-[9px] text-[13px] text-muted-foreground outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
            >
              {icon}
              {row.addLabel ?? row.label}
              {needed && (
                <span
                  aria-hidden
                  data-doc-meta-required
                  className="-ml-1 font-semibold text-destructive"
                >
                  *
                </span>
              )}
            </button>
          ) : null;
        }
        const url = row.meta!.icon === "url";
        const text = url ? docUrlHost(row.value) : row.value;
        const full = url ? docUrlWithoutUserinfo(row.value) : row.value;
        const content = (
          <>
            {icon}
            <span className="min-w-0 truncate">
              <DocText value={text} />
            </span>
          </>
        );
        const itemClass =
          "inline-flex min-w-0 max-w-[min(100%,24rem)] items-center gap-1.5";
        return (
          <Fragment key={row.key}>
            {i > 0 && !rows[i - 1]!.unset && (
              <span aria-hidden className="select-none">
                ·
              </span>
            )}
            {editable ? (
              <button
                type="button"
                title={full}
                aria-label={`${row.label}: ${full}. Change in ${name}'s settings`}
                onClick={open}
                className={cn(
                  itemClass,
                  "rounded px-0.5 outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring",
                )}
              >
                {content}
              </button>
            ) : (
              <span title={full} className={itemClass}>
                {content}
              </span>
            )}
          </Fragment>
        );
      })}
    </div>
  );
}
