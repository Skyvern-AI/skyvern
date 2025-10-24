import { create } from "zustand";
import { ProxyLocation } from "@/api/types";

export interface WorkflowModel {
  model_name: string;
}

export interface WorkflowSettingsState {
  webhookCallbackUrl: string;
  proxyLocation: ProxyLocation;
  persistBrowserSession: boolean;
  model: WorkflowModel | null;
  maxScreenshotScrollingTimes: number | null;
  extraHttpHeaders: string | Record<string, unknown> | null;
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

const defaultState: Omit<
  WorkflowSettingsState,
  "setWorkflowSettings" | "resetWorkflowSettings"
> = {
  webhookCallbackUrl: "",
  proxyLocation: ProxyLocation.Residential,
  persistBrowserSession: false,
  model: null,
  maxScreenshotScrollingTimes: null,
  extraHttpHeaders: null,
};

export const useWorkflowSettingsStore = create<WorkflowSettingsState>(
  (set) => ({
    ...defaultState,
    setWorkflowSettings: (settings) =>
      set((state) => ({ ...state, ...settings })),
    resetWorkflowSettings: () => set({ ...defaultState }),
  }),
);
