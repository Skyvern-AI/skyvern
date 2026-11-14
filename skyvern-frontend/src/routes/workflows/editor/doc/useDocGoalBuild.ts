import { useEmberActionStore } from "@/store/useEmberActionStore";

/** A Code step's Goal build in flight for this label, if any. */
export function useDocGoalBuild(label: string): "building" | "queued" | null {
  return useEmberActionStore((state) =>
    state.generatingBlockLabel === label
      ? "building"
      : state.queuedBuilds.some((queued) => queued.blockLabel === label)
        ? "queued"
        : null,
  );
}
