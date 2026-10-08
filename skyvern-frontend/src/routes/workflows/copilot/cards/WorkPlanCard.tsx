import { ListBulletIcon } from "@radix-ui/react-icons";
import { useEffect, useState } from "react";

import { CardBody, CardHeader, CopilotCard, GutterRow } from "./cardChrome";

interface WorkPlanCardProps {
  items: string[];
  // The plan this one replaced. A step counts as new only when no earlier step reads exactly the
  // same: a reworded step is not recognizable without guessing, so it shows as new plus removed.
  previous?: string[] | null;
  // The plan in force with nothing sent since. It arrives open and folds once the user moves on.
  current?: boolean;
}

function stepCount(count: number): string {
  return `${count} ${count === 1 ? "step" : "steps"}`;
}

export function WorkPlanCard({
  items,
  previous = null,
  current = true,
}: WorkPlanCardProps) {
  const [expanded, setExpanded] = useState(current);
  useEffect(() => {
    if (!current) setExpanded(false);
  }, [current]);

  const earlier = new Set(previous ?? []);
  const kept = new Set(items);
  const added = previous === null ? [] : items.filter((i) => !earlier.has(i));
  const removed = (previous ?? []).filter((item) => !kept.has(item));

  if (items.length === 0) {
    if (removed.length === 0) return null;
    return (
      <CopilotCard>
        <CardHeader
          icon={
            <ListBulletIcon className="h-3.5 w-3.5 text-muted-foreground" />
          }
          title="Plan cleared"
        />
      </CopilotCard>
    );
  }

  const changes = [
    added.length > 0 ? `${added.length} new` : null,
    removed.length > 0 ? `${removed.length} removed` : null,
  ].filter(Boolean);
  return (
    <CopilotCard>
      <div role="group" aria-label="Copilot's plan">
        <CardHeader
          icon={
            <ListBulletIcon className="h-3.5 w-3.5 text-muted-foreground" />
          }
          title={changes.length > 0 ? "Plan updated" : "Plan"}
          meta={[stepCount(items.length), ...changes].join(" · ")}
          expanded={expanded}
          onToggle={() => setExpanded((value) => !value)}
        />
        {expanded ? (
          <CardBody>
            <ol className="max-h-[60vh] overflow-y-auto">
              {items.map((item, index) => (
                <li key={`${index}-${item}`}>
                  <GutterRow marker={index + 1}>
                    <span className="whitespace-pre-wrap">{item}</span>
                    {added.includes(item) ? (
                      <span className="ml-2 whitespace-nowrap rounded-full bg-sky-500/10 px-1.5 py-0.5 text-[10px] font-medium text-sky-700 dark:text-sky-300">
                        New
                      </span>
                    ) : null}
                  </GutterRow>
                </li>
              ))}
            </ol>
            {removed.map((item) => (
              <GutterRow
                key={`removed-${item}`}
                marker="−"
                markerClass="text-destructive"
                srLabel="Removed"
              >
                <span className="whitespace-pre-wrap text-muted-foreground line-through">
                  {item}
                </span>
              </GutterRow>
            ))}
          </CardBody>
        ) : null}
      </div>
    </CopilotCard>
  );
}
