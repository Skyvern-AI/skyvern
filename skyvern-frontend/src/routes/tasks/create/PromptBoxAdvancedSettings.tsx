import { KeyValueInput } from "@/components/KeyValueInput";
import { ProxySelector } from "@/components/ProxySelector";
import { TestWebhookDialog } from "@/components/TestWebhookDialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { flushBufferedEditorEdits } from "@/hooks/useDeferredLockedEdit";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import {
  MAX_SCREENSHOT_SCROLLS_DEFAULT,
  MAX_STEPS_DEFAULT,
} from "@/routes/workflows/editor/nodes/Taskv2Node/types";
import {
  formatGeoTargetCompact,
  proxyLocationToGeoTarget,
} from "@/util/geoData";
import { Cross2Icon, GearIcon } from "@radix-ui/react-icons";
import { useState } from "react";
import {
  DEFAULT_TASK_RUN_SETTINGS,
  type SettingsTab,
  type TaskRunSettings,
} from "./taskRunSettings";

type SettingKey = keyof TaskRunSettings;

type FieldDef = {
  label: string;
  description: string;
  tab: SettingsTab;
  chip: (settings: TaskRunSettings) => string;
};

const FIELDS: Record<SettingKey, FieldDef> = {
  proxyLocation: {
    label: "Proxy Location",
    description: "Route Skyvern through one of our available proxies.",
    tab: "run",
    chip: (s) =>
      formatGeoTargetCompact(proxyLocationToGeoTarget(s.proxyLocation)),
  },
  maxStepsOverride: {
    label: "Max Steps",
    description: "The maximum number of steps to take for this task.",
    tab: "run",
    chip: (s) => `Max steps: ${s.maxStepsOverride}`,
  },
  totpIdentifier: {
    label: "2FA Identifier",
    description: "The identifier for a 2FA code for this task.",
    tab: "run",
    chip: (s) => `2FA: ${s.totpIdentifier}`,
  },
  webhookCallbackUrl: {
    label: "Webhook Callback URL",
    description: "The URL of a webhook endpoint to send the extracted data to.",
    tab: "output",
    chip: () => "Webhook",
  },
  dataSchema: {
    label: "Data Schema",
    description: "The output data schema, in JSON format.",
    tab: "output",
    chip: () => "Data schema",
  },
  generateScript: {
    label: "Generate Script",
    description: "Generate a script for this task run when it succeeds.",
    tab: "output",
    chip: () => "Generate script",
  },
  publishWorkflow: {
    label: "Publish Agent",
    description: "Create an agent alongside this task run.",
    tab: "output",
    chip: () => "Publish agent",
  },
  extraHttpHeaders: {
    label: "Extra HTTP Headers",
    description: "Extra HTTP headers sent with the browser's requests.",
    tab: "browser",
    chip: () => "HTTP headers",
  },
  maxScreenshotScrolls: {
    label: "Max Screenshot Scrolls",
    description:
      "The maximum number of scrolls for the post-action screenshot. Set 0 to capture only the viewport.",
    tab: "browser",
    chip: (s) => `Screenshot scrolls: ${s.maxScreenshotScrolls}`,
  },
};

const TABS: Array<{ value: SettingsTab; label: string }> = [
  { value: "run", label: "Run" },
  { value: "output", label: "Output" },
  { value: "browser", label: "Browser" },
];

const SETTING_KEYS = Object.keys(FIELDS) as SettingKey[];

function isChanged(key: SettingKey, settings: TaskRunSettings): boolean {
  const value = settings[key];
  if (key === "proxyLocation") {
    return value !== DEFAULT_TASK_RUN_SETTINGS.proxyLocation;
  }
  if (typeof value === "boolean") {
    return value;
  }
  return typeof value === "string" && value.trim() !== "";
}

function changedKeys(settings: TaskRunSettings): SettingKey[] {
  return SETTING_KEYS.filter((key) => isChanged(key, settings));
}

function settingsTabFor(key: SettingKey): SettingsTab {
  return FIELDS[key].tab;
}

type SettingsChangeProps = {
  settings: TaskRunSettings;
  onChange: (settings: TaskRunSettings) => void;
};

function SettingControl({
  settingKey,
  settings,
  onChange,
}: SettingsChangeProps & { settingKey: SettingKey }) {
  const set = <K extends SettingKey>(key: K, value: TaskRunSettings[K]) =>
    onChange({ ...settings, [key]: value });

  switch (settingKey) {
    case "proxyLocation":
      return (
        <ProxySelector
          modalPopover
          value={settings.proxyLocation}
          onChange={(value) => set("proxyLocation", value)}
        />
      );
    case "maxStepsOverride":
      return (
        <Input
          inputMode="numeric"
          value={settings.maxStepsOverride ?? ""}
          placeholder={`Default: ${MAX_STEPS_DEFAULT}`}
          onChange={(event) => set("maxStepsOverride", event.target.value)}
        />
      );
    case "totpIdentifier":
      return (
        <Input
          value={settings.totpIdentifier}
          onChange={(event) => set("totpIdentifier", event.target.value)}
        />
      );
    case "webhookCallbackUrl":
      return (
        <div className="flex gap-2">
          <Input
            value={settings.webhookCallbackUrl ?? ""}
            onChange={(event) => set("webhookCallbackUrl", event.target.value)}
          />
          <TestWebhookDialog
            runType="task"
            runId={null}
            initialWebhookUrl={settings.webhookCallbackUrl ?? undefined}
            trigger={
              <Button
                type="button"
                variant="secondary"
                disabled={!settings.webhookCallbackUrl}
              >
                Test
              </Button>
            }
          />
        </div>
      );
    case "dataSchema":
      return (
        <CodeEditor
          value={settings.dataSchema ?? ""}
          onChange={(value) => set("dataSchema", value || null)}
          language="json"
          minHeight="96px"
          maxHeight="240px"
          fontSize={12}
        />
      );
    case "generateScript":
    case "publishWorkflow":
      return (
        <Switch
          checked={settings[settingKey]}
          onCheckedChange={(checked) => set(settingKey, Boolean(checked))}
        />
      );
    case "extraHttpHeaders":
      return (
        <KeyValueInput
          value={settings.extraHttpHeaders ?? ""}
          onChange={(value) =>
            set(
              "extraHttpHeaders",
              value === null
                ? null
                : typeof value === "string"
                  ? value || null
                  : JSON.stringify(value),
            )
          }
          addButtonText="Add Header"
        />
      );
    case "maxScreenshotScrolls":
      return (
        <Input
          inputMode="numeric"
          value={settings.maxScreenshotScrolls ?? ""}
          placeholder={`Default: ${MAX_SCREENSHOT_SCROLLS_DEFAULT}`}
          onChange={(event) => set("maxScreenshotScrolls", event.target.value)}
        />
      );
  }
}

function SettingField(props: SettingsChangeProps & { settingKey: SettingKey }) {
  const { label, description } = FIELDS[props.settingKey];
  const heading = (
    <div>
      <div className="text-sm text-foreground">{label}</div>
      <div className="text-xs text-muted-foreground">{description}</div>
    </div>
  );
  if (
    props.settingKey === "generateScript" ||
    props.settingKey === "publishWorkflow"
  ) {
    return (
      <div className="flex items-start justify-between gap-4">
        {heading}
        <SettingControl {...props} />
      </div>
    );
  }
  return (
    <div className="space-y-1.5">
      {heading}
      <SettingControl {...props} />
    </div>
  );
}

type AdvancedSettingsPopoverProps = SettingsChangeProps & {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  tab: SettingsTab;
  onTabChange: (tab: SettingsTab) => void;
};

function AdvancedSettingsPopover({
  settings,
  onChange,
  open,
  onOpenChange,
  tab,
  onTabChange,
}: AdvancedSettingsPopoverProps) {
  const changedCount = changedKeys(settings).length;
  // KeyValueInput keeps its own rows after mount, so Reset remounts the fields.
  const [resetCount, setResetCount] = useState(0);
  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        // CodeEditor debounces its onChange; save a pending Data Schema edit
        // before closing or switching tabs unmounts the editor.
        if (!next) flushBufferedEditorEdits();
        onOpenChange(next);
      }}
    >
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={
            changedCount > 0
              ? `Advanced settings, ${changedCount} changed`
              : "Advanced settings"
          }
          className="relative flex items-center justify-center rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground data-[state=open]:bg-muted data-[state=open]:text-foreground"
        >
          <GearIcon aria-hidden="true" className="size-5 shrink-0" />
          {changedCount > 0 ? (
            <span
              aria-hidden="true"
              className="absolute -right-0.5 -top-0.5 flex size-4 items-center justify-center rounded-full bg-primary text-[10px] font-medium text-primary-foreground"
            >
              {changedCount}
            </span>
          ) : null}
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        sideOffset={12}
        className="w-[min(400px,calc(100vw-2rem))] p-0"
        onOpenAutoFocus={(event) => {
          // Radix would focus the tab panel, and Chrome paints its focus ring on a
          // scripted focus. The container keeps focus inside with no ring.
          event.preventDefault();
          (event.currentTarget as HTMLElement).focus();
        }}
      >
        <Tabs
          value={tab}
          onValueChange={(value) => {
            flushBufferedEditorEdits();
            onTabChange(value as SettingsTab);
          }}
        >
          <div className="flex items-center justify-between border-b px-3 py-2">
            <TabsList className="h-8">
              {TABS.map(({ value, label }) => {
                const tabChanged = changedKeys(settings).some(
                  (key) => settingsTabFor(key) === value,
                );
                return (
                  <TabsTrigger
                    key={value}
                    value={value}
                    className="h-6 text-xs"
                  >
                    {label}
                    {tabChanged ? (
                      <>
                        <span
                          aria-hidden="true"
                          className="ml-1.5 size-1.5 rounded-full bg-primary"
                        />
                        <span className="sr-only">, has changes</span>
                      </>
                    ) : null}
                  </TabsTrigger>
                );
              })}
            </TabsList>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-7 px-2 text-xs text-muted-foreground"
              disabled={changedCount === 0}
              onClick={() => {
                onChange(DEFAULT_TASK_RUN_SETTINGS);
                setResetCount((count) => count + 1);
              }}
            >
              Reset
            </Button>
          </div>
          {TABS.map(({ value }) => (
            <TabsContent
              key={`${value}-${resetCount}`}
              value={value}
              className="mt-0 max-h-[min(60vh,480px)] space-y-4 overflow-y-auto p-4"
            >
              {SETTING_KEYS.filter((key) => settingsTabFor(key) === value).map(
                (key) => (
                  <SettingField
                    key={key}
                    settingKey={key}
                    settings={settings}
                    onChange={onChange}
                  />
                ),
              )}
            </TabsContent>
          ))}
        </Tabs>
      </PopoverContent>
    </Popover>
  );
}

function ChangedSettingsChips({
  settings,
  onChange,
  onEdit,
}: SettingsChangeProps & { onEdit: (tab: SettingsTab) => void }) {
  const keys = changedKeys(settings);
  if (keys.length === 0) {
    return null;
  }
  return (
    <div className="mt-2 flex flex-wrap items-center gap-1.5 px-1">
      {keys.map((key) => (
        <span
          key={key}
          className="inline-flex items-center gap-1 rounded-full border border-input bg-background/80 py-0.5 pl-2.5 pr-1 text-xs text-foreground"
        >
          <button
            type="button"
            className="max-w-48 truncate hover:underline"
            onClick={() => onEdit(settingsTabFor(key))}
          >
            {FIELDS[key].chip(settings)}
          </button>
          <button
            type="button"
            aria-label={`Reset ${FIELDS[key].label}`}
            className="rounded-full p-0.5 text-muted-foreground hover:bg-muted hover:text-foreground"
            onClick={() =>
              onChange({ ...settings, [key]: DEFAULT_TASK_RUN_SETTINGS[key] })
            }
          >
            <Cross2Icon aria-hidden="true" className="size-3" />
          </button>
        </span>
      ))}
      {keys.length > 1 ? (
        <button
          type="button"
          className="text-xs text-muted-foreground hover:text-foreground"
          onClick={() => onChange(DEFAULT_TASK_RUN_SETTINGS)}
        >
          Reset all
        </button>
      ) : null}
    </div>
  );
}

export { AdvancedSettingsPopover, ChangedSettingsChips };
