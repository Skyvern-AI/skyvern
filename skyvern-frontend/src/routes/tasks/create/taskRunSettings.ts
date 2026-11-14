import { ProxyLocation } from "@/api/types";

export type TaskRunSettings = {
  proxyLocation: ProxyLocation;
  maxStepsOverride: string | null;
  totpIdentifier: string;
  webhookCallbackUrl: string | null;
  dataSchema: string | null;
  publishWorkflow: boolean;
  extraHttpHeaders: string | null;
  maxScreenshotScrolls: string | null;
};

export type SettingsTab = "run" | "output" | "browser";

export const DEFAULT_TASK_RUN_SETTINGS: TaskRunSettings = {
  proxyLocation: ProxyLocation.Residential,
  maxStepsOverride: null,
  totpIdentifier: "",
  webhookCallbackUrl: null,
  dataSchema: null,
  publishWorkflow: false,
  extraHttpHeaders: null,
  maxScreenshotScrolls: null,
};
