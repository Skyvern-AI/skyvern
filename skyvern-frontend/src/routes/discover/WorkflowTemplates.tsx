import { useMemo, useState } from "react";
import { ChevronDownIcon } from "@radix-ui/react-icons";
import { Skeleton } from "@/components/ui/skeleton";
import { useWorkflowStudioEnabled } from "@/hooks/useWorkflowStudioEnabled";
import { workflowEditorPath } from "@/routes/workflows/studioNavigation";
import { useGlobalWorkflowsQuery } from "../workflows/hooks/useGlobalWorkflowsQuery";
import { useCreateWorkflowMutation } from "../workflows/hooks/useCreateWorkflowMutation";
import { convert } from "../workflows/editor/workflowEditorUtils";
import { TEMPLATE_VIA } from "../workflows/templateGuidance";
import { WorkflowTemplateCard } from "./WorkflowTemplateCard";
import { HomeTelemetry } from "@/util/homeTelemetry";
import { TEMPORARY_TEMPLATE_IMAGES } from "./TemporaryTemplateImages";
import {
  CATEGORY_ORDER,
  TEMPLATE_CATEGORIES,
  categorizeTemplate,
  popularityRank,
  type TemplateCategory,
} from "./templateCatalog";
import { cn } from "@/util/utils";

const COLLAPSED_COUNT = 6;

type Filter = TemplateCategory | "all";

function WorkflowTemplates({ folderId }: { folderId?: string | null } = {}) {
  const { data: workflowTemplates, isLoading } = useGlobalWorkflowsQuery();
  const studioEnabled = useWorkflowStudioEnabled();
  const createWorkflow = useCreateWorkflowMutation();
  const [filter, setFilter] = useState<Filter>("all");
  const [expanded, setExpanded] = useState(false);

  const templates = useMemo(
    () =>
      (workflowTemplates ?? [])
        .map((workflow, index) => ({
          workflow,
          index,
          category: categorizeTemplate(workflow.title),
        }))
        .sort(
          (a, b) =>
            popularityRank(a.workflow.workflow_permanent_id) -
              popularityRank(b.workflow.workflow_permanent_id) ||
            a.index - b.index,
        ),
    [workflowTemplates],
  );

  if (isLoading) {
    return (
      <div className="flex gap-3 overflow-hidden md:grid md:grid-cols-2 md:gap-4 lg:grid-cols-3">
        <Skeleton className="h-80 w-64 shrink-0 rounded-2xl md:w-auto" />
        <Skeleton className="h-80 w-64 shrink-0 rounded-2xl md:w-auto" />
        <Skeleton className="h-80 w-64 shrink-0 rounded-2xl md:w-auto" />
      </div>
    );
  }

  if (templates.length === 0) {
    return null;
  }

  const filters = [
    { id: "all" as const, label: "All", count: templates.length },
    ...CATEGORY_ORDER.map((category) => ({
      id: category,
      label: TEMPLATE_CATEGORIES[category].filterLabel,
      count: templates.filter((t) => t.category === category).length,
    })).filter((option) => option.count > 0),
  ];
  const activeFilter = filters.some((option) => option.id === filter)
    ? filter
    : "all";
  const visible = templates.filter(
    (t) => activeFilter === "all" || t.category === activeFilter,
  );
  const canExpand = visible.length > COLLAPSED_COUNT;

  return (
    <section>
      <div
        data-hint="start-template"
        className="mb-3.5 flex items-start justify-between gap-3 md:items-end"
      >
        <div className="flex flex-col gap-1">
          <h2 className="text-lg font-semibold leading-7 tracking-tight md:text-xl">
            Start from a template
          </h2>
          <p className="text-[13px] text-muted-foreground">
            Ready-made agents you can run as they are or edit.
          </p>
        </div>
        {canExpand ? (
          <button
            type="button"
            aria-expanded={expanded}
            onClick={() => setExpanded((value) => !value)}
            className="hidden h-9 shrink-0 items-center gap-1.5 rounded-lg px-2 text-[13px] font-medium text-foreground transition-colors hover:bg-muted md:flex"
          >
            <ChevronDownIcon
              aria-hidden="true"
              className={cn("size-3.5 transition-transform", {
                "rotate-180": expanded,
              })}
            />
            {expanded ? "Show fewer" : `See all ${visible.length}`}
          </button>
        ) : null}
      </div>
      {filters.length > 2 ? (
        <div className="-mx-4 mb-3 flex scroll-px-4 gap-2 overflow-x-auto px-4 py-1 md:mx-0 md:flex-wrap md:px-0">
          {filters.map((option) => {
            const active = option.id === activeFilter;
            return (
              <button
                key={option.id}
                type="button"
                aria-pressed={active}
                onClick={() => {
                  setFilter(option.id);
                  setExpanded(false);
                }}
                className={cn(
                  "h-10 shrink-0 whitespace-nowrap rounded-full border px-3.5 text-[13px] font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40 md:h-9",
                  active
                    ? "border-cta bg-cta text-cta-foreground"
                    : "border-border/70 text-muted-foreground hover:bg-muted hover:text-foreground",
                )}
              >
                {option.label}{" "}
                <span className="opacity-60">{option.count}</span>
              </button>
            );
          })}
        </div>
      ) : null}
      <div className="-mx-4 -mt-2 flex snap-x snap-mandatory scroll-px-4 gap-3 overflow-x-auto px-4 pb-2 pt-2 md:mx-0 md:mt-0 md:grid md:grid-cols-2 md:gap-4 md:overflow-visible md:px-0 md:py-0 lg:grid-cols-3">
        {visible.map(({ workflow, category }, index) => (
          <WorkflowTemplateCard
            key={workflow.workflow_permanent_id}
            title={workflow.title}
            image={TEMPORARY_TEMPLATE_IMAGES[workflow.workflow_permanent_id]}
            category={category}
            popular={popularityRank(workflow.workflow_permanent_id) === 0}
            to={workflowEditorPath(
              workflow.workflow_permanent_id,
              studioEnabled,
            )}
            onClick={(event) => {
              // The card calls onClick without an event for middle clicks; that must stay a non-plain click.
              const plainClick =
                !!event &&
                event.button === 0 &&
                !event.metaKey &&
                !event.ctrlKey &&
                !event.shiftKey &&
                !event.altKey;
              if (plainClick && createWorkflow.isPending) {
                event.preventDefault();
                return;
              }
              HomeTelemetry.templateClicked({
                workflowPermanentId: workflow.workflow_permanent_id,
                title: workflow.title,
              });
              // Modified and middle clicks keep opening the read-only template.
              if (!plainClick) return;
              event.preventDefault();
              createWorkflow.mutate({
                ...convert({ ...workflow, title: `${workflow.title} (copy)` }),
                folder_id: folderId,
                _via: TEMPLATE_VIA,
              });
            }}
            className={cn("w-64 shrink-0 snap-start md:w-auto", {
              "md:hidden": !expanded && index >= COLLAPSED_COUNT,
            })}
          />
        ))}
      </div>
    </section>
  );
}

export { WorkflowTemplates };
