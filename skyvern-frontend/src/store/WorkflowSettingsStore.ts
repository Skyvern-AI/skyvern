import { create } from "zustand";
import { ProxyLocation } from "@/api/types";

export interface WorkflowModel {
  model_name: string;
}

export interface WorkflowSettingsState {
  webhookCallbackUrl: string;
  proxyLocation: ProxyLocation;
  persistBrowserSession: boolean;
  reuseBrowserSession: boolean;
  pinSavedSessionIp: boolean;
  browserProfileKey: string | null;
  model: WorkflowModel | null;
  maxScreenshotScrollingTimes: number | null;
  extraHttpHeaders: string | Record<string, unknown> | null;
  finallyBlockLabel: string | null;
  setWorkflowSettings: (
    settings: Partial<
      Omit<
        WorkflowSettingsState,
        "setWorkflowSettings" | "resetWorkflowSettings"
      >
    >,
  ) => void;
  resetWorkflowSettings: () => void;
}

export type WorkflowBrowserSessionReuseUpdate = Pick<
  WorkflowSettingsState,
  "persistBrowserSession" | "reuseBrowserSession"
>;

export function updateWorkflowBrowserSessionReuse(
  enabled: boolean,
  persistBrowserSession: boolean,
  updateSettings: (settings: WorkflowBrowserSessionReuseUpdate) => void,
): void {
  updateSettings({
    reuseBrowserSession: enabled,
    persistBrowserSession: enabled || persistBrowserSession,
  });
}

const defaultState: Omit<
  WorkflowSettingsState,
  "setWorkflowSettings" | "resetWorkflowSettings"
> = {
  webhookCallbackUrl: "",
  proxyLocation: ProxyLocation.Residential,
  persistBrowserSession: false,
  reuseBrowserSession: false,
  pinSavedSessionIp: false,
  browserProfileKey: null,
  model: null,
  maxScreenshotScrollingTimes: null,
  extraHttpHeaders: null,
  finallyBlockLabel: null,
};

export const useWorkflowSettingsStore = create<WorkflowSettingsState>(
  (set) => ({
    ...defaultState,
    setWorkflowSettings: (settings) =>
      set((state) => ({ ...state, ...settings })),
    resetWorkflowSettings: () => set({ ...defaultState }),
  }),
);
