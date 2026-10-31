import type { ReviewProjection } from "../narrativeState";
import { humanizeBlockLabel } from "../blockLabel";
import { GutterRow } from "./cardChrome";

type ReviewedBlock = ReviewProjection["blocks"][number];

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
