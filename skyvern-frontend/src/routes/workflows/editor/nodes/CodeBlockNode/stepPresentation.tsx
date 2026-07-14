import type { ReactNode } from "react";

import { getReadableActionType } from "@/api/types";
import { getActionTypeIcon } from "@/routes/workflows/components/actionTypeIcons";

export function getStepIcon(actionType: string): ReactNode {
  return getActionTypeIcon(actionType);
}

export function getStepLabel(actionType: string): string {
  return getReadableActionType(actionType);
}

const chipTintByActionType: Record<string, string> = {
  goto_url: "bg-sky-500/10 text-sky-700 dark:text-sky-300",
  go_back: "bg-sky-500/10 text-sky-700 dark:text-sky-300",
  go_forward: "bg-sky-500/10 text-sky-700 dark:text-sky-300",
  reload_page: "bg-sky-500/10 text-sky-700 dark:text-sky-300",
  new_tab: "bg-sky-500/10 text-sky-700 dark:text-sky-300",
  switch_tab: "bg-sky-500/10 text-sky-700 dark:text-sky-300",
  close_page: "bg-sky-500/10 text-sky-700 dark:text-sky-300",
  scroll: "bg-sky-500/10 text-sky-700 dark:text-sky-300",
  extract: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
  verification_code: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
  complete: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
  execute_js: "bg-violet-500/10 text-violet-700 dark:text-violet-300",
  wait: "bg-amber-500/10 text-amber-700 dark:text-amber-300",
  solve_captcha: "bg-amber-500/10 text-amber-700 dark:text-amber-300",
  terminate: "bg-rose-500/10 text-rose-700 dark:text-rose-300",
};

const defaultChipTint = "bg-slate-500/10 text-slate-700 dark:text-slate-300";

export function getStepChipClassName(actionType: string): string {
  return chipTintByActionType[actionType] ?? defaultChipTint;
}
