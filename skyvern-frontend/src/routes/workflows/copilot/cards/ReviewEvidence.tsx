import type { ReviewChange, ReviewProjection } from "../narrativeState";
import { humanizeBlockLabel } from "../blockLabel";
import { GutterRow } from "./cardChrome";

type ReviewedBlock = ReviewProjection["blocks"][number];

// eslint-disable-next-line react-refresh/only-export-components
export const CHANGE_MARKERS: Record<
  ReviewChange,
  { marker: string; srLabel: string; markerClass: string }
> = {
  added: { marker: "+", srLabel: "Added", markerClass: "text-success" },
  changed: {
    marker: "~",
    srLabel: "Changed",
    markerClass: "text-amber-700 dark:text-amber-300",
  },
  removed: { marker: "−", srLabel: "Removed", markerClass: "text-destructive" },
  unchanged: {
    marker: "",
    srLabel: "Unchanged",
    markerClass: "text-muted-foreground",
  },
};

// eslint-disable-next-line react-refresh/only-export-components
export function reviewCoverageTag(
  block: ReviewedBlock,
  tested: boolean,
): string | null {
  if (tested) return null;
  if (block.coverage === "different_source") return "Different source";
  if (block.coverage === "unknown") return "Not tested under this name";
  if (
    block.coverage === "never_run" ||
    (block.coverage === undefined && block.neverTested)
  ) {
    return "Never tested";
  }
  return null;
}

export function ReviewCoverageBadge({ label }: { label: string | null }) {
  return label ? (
    <span className="ml-2 whitespace-nowrap rounded-full bg-sky-500/10 px-1.5 py-0.5 text-[10px] font-medium text-sky-700 dark:text-sky-300">
      {label}
    </span>
  ) : null;
}

// eslint-disable-next-line react-refresh/only-export-components
export function humanizedList(labels: string[]): string {
  const names = labels.map(humanizeBlockLabel);
  if (names.length < 2) return names[0] ?? "";
  if (names.length === 2) return `${names[0]} and ${names[1]}`;
  return `${names.slice(0, -1).join(", ")}, and ${names[names.length - 1]}`;
}

export function DuplicateWriteRows({
  groups,
}: {
  groups: ReviewProjection["duplicateWrites"];
}) {
  return groups.map((group) => (
    <GutterRow
      key={`${group.blockType}-${group.blockLabels.join("-")}`}
      marker="!"
      markerClass="font-bold text-amber-700 dark:text-amber-300"
      srLabel="Warning"
    >
      <span className="text-amber-700 dark:text-amber-300">
        {humanizedList(group.blockLabels)} write to the same destination.
      </span>
    </GutterRow>
  ));
}

export function InputChangeRows({
  inputs,
  discarded,
}: {
  inputs: NonNullable<ReviewProjection["inputs"]>;
  discarded: boolean;
}) {
  return inputs.map((input) => {
    const marker = CHANGE_MARKERS[input.change];
    return (
      <GutterRow
        key={`input-${input.change}-${input.key}`}
        marker={marker.marker}
        markerClass={marker.markerClass}
        srLabel={marker.srLabel}
      >
        <span
          className={
            discarded || input.change === "removed"
              ? "text-muted-foreground line-through"
              : ""
          }
        >
          Input{" "}
          <span className="font-mono [overflow-wrap:anywhere]">
            {input.key}
          </span>
        </span>
      </GutterRow>
    );
  });
}
