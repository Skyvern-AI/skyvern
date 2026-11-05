import { useEffect, useState } from "react";
import type { DocStarterProgress } from "./docStarterTips";

const STORE_KEY = "skyvern.doc.gettingStarted";

type StarterState = {
  open: string[];
  dismissed: string[];
  seenTips: string[];
  done: Record<string, string[]>;
};

function readState(): StarterState {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORE_KEY) ?? "{}");
    return {
      open: Array.isArray(parsed.open) ? parsed.open : [],
      dismissed: Array.isArray(parsed.dismissed) ? parsed.dismissed : [],
      seenTips: Array.isArray(parsed.seenTips) ? parsed.seenTips : [],
      done:
        parsed.done &&
        typeof parsed.done === "object" &&
        !Array.isArray(parsed.done)
          ? parsed.done
          : {},
    };
  } catch {
    return { open: [], dismissed: [], seenTips: [], done: {} };
  }
}

function writeState(state: StarterState) {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(state));
  } catch {
    // Without storage the checklist still shows for this visit.
  }
}

/**
 * The checklist and tips for an agent that was opened in the Doc with no steps.
 * Progress is read from the agent itself; only "started here" and dismissals are remembered.
 */
export function useDocStarter(workflowId: string | null, startsEmpty: boolean) {
  const [state, setState] = useState<StarterState>(readState);
  const key = workflowId ?? "";
  useEffect(() => {
    if (!key || !startsEmpty) return;
    setState((current) => {
      if (current.open.includes(key) || current.dismissed.includes(key))
        return current;
      const next = { ...current, open: [...current.open, key].slice(-50) };
      writeState(next);
      return next;
    });
  }, [key, startsEmpty]);
  const update = (change: (current: StarterState) => StarterState) =>
    setState((current) => {
      const next = change(current);
      writeState(next);
      return next;
    });
  return {
    active: Boolean(key) && state.open.includes(key),
    seenTips: state.seenTips,
    done: state.done[key] ?? [],
    markDone: (items: string[]) =>
      setState((current) => {
        const have = current.done[key] ?? [];
        if (items.every((item) => have.includes(item))) return current;
        const next = {
          ...current,
          done: { ...current.done, [key]: [...new Set([...have, ...items])] },
        };
        writeState(next);
        return next;
      }),
    hide: () =>
      update((current) => ({
        ...current,
        open: current.open.filter((id) => id !== key),
        dismissed: [...current.dismissed.filter((id) => id !== key), key].slice(
          -50,
        ),
      })),
    seeTip: (tip: string) =>
      update((current) => ({
        ...current,
        seenTips: [...current.seenTips, tip],
      })),
  };
}

export type DocStarterItemId = "step" | "reference" | "nest" | "run";

/** The checklist items the draft satisfies right now; once ticked, an item stays ticked. */
export function docStarterDone(
  progress: DocStarterProgress,
): DocStarterItemId[] {
  const done: DocStarterItemId[] = [];
  if (progress.steps > 0) done.push("step");
  if (progress.usedReference) done.push("reference");
  if (progress.nested) done.push("nest");
  if (progress.ran) done.push("run");
  return done;
}
